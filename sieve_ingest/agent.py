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

        sources_changed = urls_changed = objects_written = 0
        for s in sources:
            try:
                changes = freshness.detect(conn, s)
            except Exception as e:
                log.warning('detect failed for %s: %s', s['source_id'], e)
                changes = []
            if changes:
                sources_changed += 1
            for ch in changes[:MAX_URLS_PER_SOURCE]:
                urls_changed += 1
                db.record_change(conn, run_id, s['source_id'], ch.url,
                                 ch.change_type, ch.signal, ch.old_hash, ch.new_hash)
                counts = extract.ingest_page(conn, ch, s)
                objects_written += counts.get('new', 0)
                db.save_url_state(conn, ch.url, s['source_id'], None, None, ch.new_hash)
            marker = changes[0].new_hash if (changes and s['adapter_type'] in
                                             ('github_release',)) else None
            db.mark_source_crawled(conn, s['source_id'], marker)

        db.finish_run(conn, run_id, sources_checked=len(sources),
                      sources_changed=sources_changed, urls_changed=urls_changed,
                      objects_written=objects_written)
        summary = {'run_id': run_id, 'sources_checked': len(sources),
                   'sources_changed': sources_changed, 'urls_changed': urls_changed,
                   'rules_written': objects_written}
        log.info('run %s done: %s', run_id, summary)
        return summary
    finally:
        conn.close()
