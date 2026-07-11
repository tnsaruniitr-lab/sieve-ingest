"""CLI for the ingestion agent.

    python -m sieve_ingest seed          # create schema + seed MISSING sources (insert-only)
    python -m sieve_ingest seed --force  # code→DB registry sync (overwrites all but `enabled`)
    python -m sieve_ingest run           # run one ingestion cycle (what Railway cron calls)
    python -m sieve_ingest status        # show registry + last run
    python -m sieve_ingest changes      # show recent detected changes
    python -m sieve_ingest migrate-url-state  # one-time: re-key url_state through normalize_url
"""

from __future__ import annotations

import json
import logging
import sys

from . import agent, db, registry

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s',
                    datefmt='%H:%M:%S', stream=sys.stdout)


def _status():
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT tier, source_id, canonical_org, adapter_type, crawl_cadence_days, "
                        "last_crawled_at FROM sieve.source_registry ORDER BY tier, source_id")
            print('SOURCE REGISTRY:')
            for tier, sid, org, ad, cad, last in cur.fetchall():
                print(f'  T{tier} {sid:24s} {org:22s} {ad:14s} every {cad}d  last={last}')
            cur.execute("SELECT run_id, started_at, sources_checked, sources_changed, "
                        "urls_changed, objects_written, status FROM sieve.ingest_runs "
                        "ORDER BY run_id DESC LIMIT 3")
            print('\nRECENT RUNS:')
            for r in cur.fetchall():
                print(f'  run {r[0]} {r[1]} checked={r[2]} changed_src={r[3]} '
                      f'changed_urls={r[4]} rules+={r[5]} [{r[6]}]')
    finally:
        conn.close()


def _changes():
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT detected_at, source_id, change_type, signal, "
                        "extract_status, rules_new, url "
                        "FROM sieve.ingest_changes ORDER BY change_id DESC LIMIT 25")
            for at, sid, ct, sig, st, rn, url in cur.fetchall():
                print(f'  {at}  [{sid}] {ct} via {sig} → {st} (+{rn or 0})  {url}')
    finally:
        conn.close()


def _migrate_url_state():
    """One-time deploy step: old url_state rows are keyed on RAW sitemap URLs
    (?hl=, utm, trailing slash); detection now looks up normalized keys, so
    un-migrated rows would re-detect as 'new' (a re-extraction burst). Re-keys
    every row, keeping the newest fingerprint when variants collapse."""
    from . import freshness
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT url, source_id, etag, last_modified, content_hash, "
                        "last_seen_at FROM sieve.url_state ORDER BY last_seen_at ASC")
            rows = cur.fetchall()
            migrated = dropped = 0
            for url, sid, etag, lm, ch, _seen in rows:
                norm = freshness.normalize_url(url)
                if norm == url:
                    continue
                # Later rows (newest last_seen_at) overwrite earlier variants.
                db.save_url_state(conn, norm, sid, etag, lm, ch)
                cur.execute("DELETE FROM sieve.url_state WHERE url=%s", (url,))
                migrated += 1
                dropped += 1
            print(f'url_state: {len(rows)} rows scanned, {migrated} re-keyed, '
                  f'{dropped} raw variants removed')
    finally:
        conn.close()


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'run'
    if cmd == 'seed':
        force = '--force' in sys.argv[2:]
        n = registry.seed(force=force)
        print(f"seeded {n} sources ({'force sync' if force else 'insert-only'})")
    elif cmd == 'run':
        print(json.dumps(agent.run_cycle(), indent=2))
    elif cmd == 'status':
        _status()
    elif cmd == 'changes':
        _changes()
    elif cmd == 'migrate-url-state':
        _migrate_url_state()
    else:
        print(__doc__); sys.exit(1)


if __name__ == '__main__':
    main()
