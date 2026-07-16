"""
agent.py — the ingestion/freshness orchestrator (one cycle).

Runs as a SEPARATE scheduled service from the auditor (Railway cron). One cycle:

    seed registry (idempotent)
    for each DUE source (cadence elapsed):
        detect changes (freshness adapter)         ← "what changed?"
        for each changed URL:
            extract rules → write with provenance   ← preserves source + last_verified
            save url fingerprint + change-log row
        mark source crawled (+ new marker)
    record the run

It never runs inside the auditor's request path. The auditor just reads the
brain the next time it audits.
"""

from __future__ import annotations

import logging
import os

from . import db, extract, freshness, registry

log = logging.getLogger('ingest.agent')

MAX_URLS_PER_SOURCE = int(os.getenv('MAX_URLS_PER_SOURCE', '15'))


def run_cycle() -> dict:
    conn = db.connect()
    try:
        registry.seed(conn)
        run_id = db.start_run(conn)
        sources = db.due_sources(conn)
        log.info('run %s — %d due sources', run_id, len(sources))

        # FAILURE-CLASS statuses from extract.ingest_page — anything here on a
        # page means the extraction pipeline (not the page) failed. If ANY page
        # fails this way, the run is 'degraded', never a silent 'done'.
        FAILURE_STATUSES = {'no_sdk', 'no_api_key', 'llm_error', 'parse_error',
                            'all_invalid', 'write_error'}
        # config failures are identical for every page — bail instead of burning
        # the whole backlog with one broken key
        FATAL_STATUSES = {'no_sdk', 'no_api_key'}

        sources_changed = urls_changed = objects_written = rules_refreshed = 0
        detail = {}
        any_failure = False
        for s in sources:
            src_detail = {'changed': 0, 'new': 0, 'refreshed': 0, 'dropped': 0, 'statuses': {}}
            try:
                changes = freshness.detect(conn, s)
            except Exception as e:
                log.warning('detect failed for %s: %s', s['source_id'], e)
                src_detail['detect_error'] = str(e)[:200]
                changes = []
            if changes:
                sources_changed += 1
            src_detail['changed'] = min(len(changes), MAX_URLS_PER_SOURCE)
            fatal = None
            for ch in changes[:MAX_URLS_PER_SOURCE]:
                urls_changed += 1
                db.record_change(conn, run_id, s['source_id'], ch.url,
                                 ch.change_type, ch.signal, ch.old_hash, ch.new_hash)
                counts = extract.ingest_page(conn, ch, s)
                objects_written += counts.get('new', 0)
                rules_refreshed += counts.get('refreshed', 0)
                src_detail['new'] += counts.get('new', 0)
                src_detail['refreshed'] += counts.get('refreshed', 0)
                src_detail['dropped'] += counts.get('dropped', 0)
                st = counts.get('status', 'unknown')
                src_detail['statuses'][st] = src_detail['statuses'].get(st, 0) + 1
                if st in FAILURE_STATUSES:
                    any_failure = True
                    # Do NOT advance the fingerprint on a failed extraction —
                    # leaving no state means the page retries next cycle instead
                    # of being permanently consumed with zero rules.
                    if st in FATAL_STATUSES:
                        fatal = st
                        break
                    continue
                db.save_url_state(conn, ch.url, s['source_id'], None, None, ch.new_hash)
            marker = changes[0].new_hash if (changes and s['adapter_type'] in
                                             ('github_release',)
                                             and src_detail['statuses'].get('no_text', 0) == 0
                                             ) else None
            if fatal:
                # config failure — don't mark crawled, don't touch other sources
                detail[s['source_id']] = src_detail
                log.error('FATAL extraction status %s — aborting cycle so the '
                          'backlog is preserved for retry', fatal)
                db.finish_run(conn, run_id, sources_checked=len(sources),
                              sources_changed=sources_changed, urls_changed=urls_changed,
                              objects_written=objects_written, status='failed')
                from psycopg2.extras import Json as _J
                with conn.cursor() as cur:
                    cur.execute("UPDATE sieve.ingest_runs SET detail=%s WHERE run_id=%s",
                                (_J(detail), run_id))
                return {'run_id': run_id, 'status': 'failed', 'fatal': fatal,
                        'detail': detail}
            db.mark_source_crawled(conn, s['source_id'], marker)
            if src_detail['changed'] or src_detail.get('detect_error'):
                detail[s['source_id']] = src_detail

        from psycopg2.extras import Json
        run_status = 'degraded' if any_failure else 'done'
        db.finish_run(conn, run_id, sources_checked=len(sources),
                      sources_changed=sources_changed, urls_changed=urls_changed,
                      objects_written=objects_written,
                      status=run_status, detail=Json(detail))
        summary = {'run_id': run_id, 'status': run_status,
                   'sources_checked': len(sources),
                   'sources_changed': sources_changed, 'urls_changed': urls_changed,
                   'rules_written': objects_written, 'rules_refreshed': rules_refreshed,
                   'detail': detail}
        log.info('run %s done: %s', run_id, summary)
        return summary
    finally:
        conn.close()
