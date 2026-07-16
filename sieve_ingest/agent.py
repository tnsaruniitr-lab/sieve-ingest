"""
agent.py — the ingestion/freshness orchestrator (one cycle).

Runs as a SEPARATE scheduled service from the auditor (Railway cron). One cycle:

    seed registry (insert-only — operator DB fixes survive)
    for each DUE source (cadence elapsed):
        detect changes (freshness adapter)         ← "what changed?"
        for each changed URL:
            extract rules → write with provenance   ← preserves source + last_verified
            consume the change ONLY on success      ← failures retry next cycle
        mark source crawled (skipped when detection itself failed)
    record the run with a truthful status (done | partial | failed)

Failure semantics (the load-bearing part):
  - extraction failed  → url_state NOT saved → same content version re-detected
    and re-extracted next cycle; after GAVE_UP_AFTER failures the change is
    consumed with extract_status='gave_up' so a permanently-poisoned page can't
    become a weekly retry storm.
  - detection failed   → source NOT marked crawled → retried next cycle instead
    of silently waiting out a full 7–30 day cadence.
  - the github_release marker is only advanced when the release's changes were
    all consumed, so a failed extraction doesn't eat the release.

It never runs inside the auditor's request path. The auditor just reads the
brain the next time it audits.
"""

from __future__ import annotations

import logging
import os

from . import db, extract, freshness, registry

log = logging.getLogger('ingest.agent')

MAX_URLS_PER_SOURCE = int(os.getenv('MAX_URLS_PER_SOURCE', '15'))
GAVE_UP_AFTER = int(os.getenv('GAVE_UP_AFTER', '3'))

# Observability (all optional, all best-effort — a broken alert never breaks a run):
#   HEALTHCHECK_PING_URL  dead-man's switch (e.g. healthchecks.io): pinged on
#                         success, <url>/fail on partial/failed. A MISSED ping
#                         is the signal that the cron never ran at all.
#   ALERT_WEBHOOK_URL     POSTed a JSON digest when a run isn't clean or any
#                         source hits a 3+ failure streak.
HEALTHCHECK_PING_URL = os.getenv('HEALTHCHECK_PING_URL', '')
ALERT_WEBHOOK_URL = os.getenv('ALERT_WEBHOOK_URL', '')


def _notify(summary: dict, outcomes: list) -> None:
    """Fire the dead-man ping + failure webhook. Never raises."""
    import httpx
    ok = summary.get('status') == 'done'
    streaks = [o for o in outcomes
               if o.get('consecutive_failures', 0) >= GAVE_UP_AFTER]
    if HEALTHCHECK_PING_URL:
        try:
            url = HEALTHCHECK_PING_URL if ok else HEALTHCHECK_PING_URL.rstrip('/') + '/fail'
            httpx.get(url, timeout=10)
        except Exception as e:
            log.warning('healthcheck ping failed: %s', e)
    if ALERT_WEBHOOK_URL and (not ok or streaks):
        try:
            httpx.post(ALERT_WEBHOOK_URL, timeout=10, json={
                'service': 'sieve-ingest', 'summary': summary,
                'failing_streaks': [{'source_id': o['source_id'],
                                     'consecutive_failures': o['consecutive_failures'],
                                     'error': o.get('error')} for o in streaks],
            })
        except Exception as e:
            log.warning('alert webhook failed: %s', e)

# extract_page statuses that consume the change (url_state saved).
_CONSUMED = ('extracted', 'empty', 'irrelevant')


def _process_source(conn, run_id: int, s: dict) -> dict:
    """One source: detect → extract each change → consume-or-retry. Returns the
    per-source outcome row that goes into ingest_runs.detail."""
    out = {'source_id': s['source_id'], 'status': 'ok', 'changes': 0,
           'extracted': 0, 'empty': 0, 'irrelevant': 0, 'failed': 0,
           'gave_up': 0, 'rules_new': 0, 'rules_refreshed': 0}
    try:
        changes = freshness.detect(conn, s)
    except Exception as e:
        log.warning('detect failed for %s: %s', s['source_id'], e)
        out['status'] = 'detect_failed'
        out['error'] = str(e)[:300]
        out['consecutive_failures'] = db.update_source_health(
            conn, s['source_id'], ok=False, error=f'detect: {e}')
        return out  # source NOT marked crawled — retried next cycle

    all_consumed = True
    for ch in changes[:MAX_URLS_PER_SOURCE]:
        out['changes'] += 1
        change_id = db.record_change(conn, run_id, s['source_id'], ch.url,
                                     ch.change_type, ch.signal, ch.old_hash, ch.new_hash)
        if ch.change_type == 'removed':
            # Page is gone — retire its rules (never delete), drop the
            # fingerprint so a resurrected page re-enters as 'new'.
            n = db.retire_rules_for_url(conn, ch.url)
            log.info('  %s GONE — retired %d rule(s)', ch.url, n)
            db.update_change_outcome(conn, change_id, 'retired', 0, 0)
            out['retired'] = out.get('retired', 0) + n
            continue
        try:
            counts = extract.ingest_page(conn, ch, s)
        except Exception as e:
            # One poison page must never kill the run (and would leave this row
            # 'detected' forever, invisible to the gave_up counter).
            log.warning('  %s ingest_page crashed: %s', ch.url, e)
            counts = {'new': 0, 'refreshed': 0, 'status': 'failed'}
        status = counts['status']
        out['rules_new'] += counts.get('new', 0)
        out['rules_refreshed'] += counts.get('refreshed', 0)

        if status == 'failed':
            # Backstop: after GAVE_UP_AFTER failures for this exact url+content
            # version, consume it as 'gave_up' — visible in the records, no
            # eternal retry storm. count includes this failure once recorded.
            prior = db.count_extract_failures(conn, ch.url, ch.new_hash)
            if prior + 1 >= GAVE_UP_AFTER:
                status = 'gave_up'
                log.warning('  %s failed %d times — giving up on this version',
                            ch.url, prior + 1)

        # Outcome first, consume second: a crash between the two leaves an
        # honest 'failed' row and an unconsumed change (retried), never the
        # reverse (consumed but recorded as in-flight).
        db.update_change_outcome(conn, change_id, status,
                                 counts.get('new', 0), counts.get('refreshed', 0))
        if status in _CONSUMED or status == 'gave_up':
            db.save_url_state(conn, ch.url, s['source_id'], ch.etag, ch.lastmod,
                              ch.new_hash)
        else:
            all_consumed = False  # leave url_state untouched → retry
        out[status] += 1

    if out['failed']:
        out['status'] = 'extract_failed'
        out['consecutive_failures'] = db.update_source_health(
            conn, s['source_id'], ok=False,
            error=f"{out['failed']} extraction failure(s)")
        # Do NOT mark the source crawled: the unconsumed changes must be retried
        # at the NEXT cron slot, not after a full 7-30 day cadence. Re-detection
        # of the consumed URLs is cheap (lastmod filter / 304s / hash match).
        return out

    db.update_source_health(conn, s['source_id'], ok=True)
    # Version markers (github_release) only advance when nothing was left behind,
    # so a failed extraction can't permanently eat the release.
    marker = None
    if changes and s['adapter_type'] in ('github_release',) and all_consumed:
        marker = changes[0].new_hash
    db.mark_source_crawled(conn, s['source_id'], marker)
    return out


def run_cycle() -> dict:
    conn = db.connect()
    run_id = None
    try:
        registry.seed(conn)  # insert-only: fills gaps, never clobbers operator fixes
        run_id = db.start_run(conn)
        sources = db.due_sources(conn)
        log.info('run %s — %d due sources', run_id, len(sources))

        outcomes = [_process_source(conn, run_id, s) for s in sources]

        # Same-cycle embeddings: new rules become vector-retrievable before the
        # auditor's next audit (no-op without OPENAI_API_KEY; failures defer).
        try:
            embedded = db.embed_missing_rules(conn)
        except Exception as e:
            log.warning('embedding pass failed (deferred): %s', e)
            embedded = 0

        sources_changed = sum(1 for o in outcomes if o['changes'])
        urls_changed = sum(o['changes'] for o in outcomes)
        objects_written = sum(o['rules_new'] for o in outcomes)
        clean = all(o['status'] == 'ok' and not o['gave_up'] for o in outcomes)
        status = 'done' if clean else 'partial'

        db.finish_run(conn, run_id, status=status, detail={'sources': outcomes},
                      sources_checked=len(sources), sources_changed=sources_changed,
                      urls_changed=urls_changed, objects_written=objects_written)
        summary = {'run_id': run_id, 'status': status, 'sources_checked': len(sources),
                   'sources_changed': sources_changed, 'urls_changed': urls_changed,
                   'rules_written': objects_written, 'rules_embedded': embedded,
                   'rules_retired': sum(o.get('retired', 0) for o in outcomes),
                   'failed_sources': [o['source_id'] for o in outcomes
                                      if o['status'] != 'ok']}
        log.info('run %s %s: %s', run_id, status, summary)
        _notify(summary, outcomes)
        return summary
    except Exception as e:
        # A crash must never leave the run stuck 'running' with no trace.
        if run_id is not None:
            try:
                db.finish_run(conn, run_id, status='failed',
                              detail={'error': str(e)[:500]})
            except Exception:
                log.exception('could not record failed run %s', run_id)
        _notify({'run_id': run_id, 'status': 'failed', 'error': str(e)[:300]}, [])
        raise
    finally:
        conn.close()
