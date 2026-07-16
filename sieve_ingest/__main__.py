"""CLI for the ingestion agent.

    python -m sieve_ingest seed     # create schema + seed the source registry
    python -m sieve_ingest run      # one ingestion cycle + health report (Railway cron)
    python -m sieve_ingest status   # show registry + last run
    python -m sieve_ingest changes  # show recent detected changes
    python -m sieve_ingest health   # brain coverage metrics + snapshot + alerts
    python -m sieve_ingest critic   # canon-topic completeness probe
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
            cur.execute("SELECT detected_at, source_id, change_type, signal, url "
                        "FROM sieve.ingest_changes ORDER BY change_id DESC LIMIT 25")
            for at, sid, ct, sig, url in cur.fetchall():
                print(f'  {at}  [{sid}] {ct} via {sig}  {url}')
    finally:
        conn.close()


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'run'
    if cmd == 'seed':
        n = registry.seed(); print(f'seeded {n} sources')
    elif cmd == 'run':
        summary = agent.run_cycle()
        print(json.dumps(summary, indent=2))
        # Health report rides the same cron so coverage drift is visible weekly.
        try:
            from . import health
            health.report()
        except Exception as e:
            logging.getLogger('ingest').warning('health report failed: %s', e)
    elif cmd == 'status':
        _status()
    elif cmd == 'changes':
        _changes()
    elif cmd == 'health':
        from . import health
        health.report()
    elif cmd == 'critic':
        from . import critic
        critic.run_probe()
    elif cmd == 'observe':
        from . import observe
        if len(sys.argv) < 3:
            print('usage: python -m sieve_ingest observe <observations.jsonl> [--dry-run]')
            sys.exit(1)
        observe.run(sys.argv[2], dry_run='--dry-run' in sys.argv)
    else:
        print(__doc__); sys.exit(1)


if __name__ == '__main__':
    main()
