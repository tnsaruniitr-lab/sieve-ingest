"""CLI for the ingestion agent.

    python -m sieve_ingest seed          # create schema + seed MISSING sources (insert-only)
    python -m sieve_ingest seed --force  # code→DB registry sync (overwrites all but `enabled`)
    python -m sieve_ingest run           # run one ingestion cycle (what Railway cron calls)
    python -m sieve_ingest status        # show registry + last run
    python -m sieve_ingest changes      # show recent detected changes
    python -m sieve_ingest health       # per-source health: failures, last ok, last error
    python -m sieve_ingest brain-health # brain coverage metrics + snapshot + drift alerts
    python -m sieve_ingest critic       # canon-topic completeness probe
    python -m sieve_ingest observe <f>  # ingest crawl observations (rule_type=observed)
    python -m sieve_ingest migrate-url-state  # one-time: re-key url_state through normalize_url

  Operator commands (no code deploy needed — insert-only seed never reverts them):
    python -m sieve_ingest add-source <id> canonical_org=.. root_url=.. [adapter_type=.. ...]
    python -m sieve_ingest set-source <id> <field>=<value> [...]   # fix a row in place
    python -m sieve_ingest enable <id> | disable <id>
    python -m sieve_ingest probe-source <id>   # dry-run pre-flight: NO db writes, no LLM
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


_SETTABLE = {'canonical_org', 'adapter_type', 'tier', 'root_url', 'sitemap_url',
             'url_filter', 'crawl_cadence_days', 'notes'}


def _set_source(source_id: str, pairs):
    """set-source <id> field=value [...] — operator fix, survives re-seeds."""
    import json as _json
    updates = {}
    for p in pairs:
        if '=' not in p:
            print(f'bad assignment {p!r} — use field=value'); sys.exit(1)
        k, v = p.split('=', 1)
        if k == 'seed_urls':
            updates[k] = _json.loads(v)  # JSON array
        elif k in _SETTABLE:
            updates[k] = (None if v in ('', 'null', 'NULL') else v)
        else:
            print(f'unknown field {k!r}; settable: {sorted(_SETTABLE | {"seed_urls"})}')
            sys.exit(1)
    conn = db.connect()
    try:
        from psycopg2.extras import Json
        sets, vals = [], []
        for k, v in updates.items():
            sets.append(f'{k}=%s')
            vals.append(Json(v) if k == 'seed_urls' else v)
        with conn.cursor() as cur:
            cur.execute(f"UPDATE sieve.source_registry SET {', '.join(sets)} "
                        f"WHERE source_id=%s RETURNING source_id", (*vals, source_id))
            if not cur.fetchone():
                print(f'no such source {source_id!r}'); sys.exit(1)
        print(f'{source_id}: set {", ".join(updates)}')
    finally:
        conn.close()


def _add_source(source_id: str, pairs):
    """add-source <id> canonical_org=.. root_url=.. [adapter_type=.. tier=..
    sitemap_url=.. seed_urls='[..]' url_filter=.. crawl_cadence_days=..]

    Registers a BRAND-NEW source so the next cron cycle crawls it — no code
    deploy. Inserted disabled=false won't happen; it lands ENABLED and due
    immediately (last_crawled_at NULL). Run `probe-source <id>` right after to
    confirm it actually yields pages before the cron spends LLM budget."""
    import json as _json
    row = {'source_id': source_id, 'adapter_type': 'sitemap', 'tier': 3,
           'crawl_cadence_days': 30, 'enabled': True}
    for p in pairs:
        if '=' not in p:
            print(f'bad assignment {p!r} — use field=value'); sys.exit(1)
        k, v = p.split('=', 1)
        if k == 'seed_urls':
            row[k] = _json.loads(v)
        elif k in _SETTABLE:
            row[k] = (None if v in ('', 'null', 'NULL') else v)
        else:
            print(f'unknown field {k!r}; allowed: {sorted(_SETTABLE | {"seed_urls"})}')
            sys.exit(1)
    if not row.get('canonical_org') or not row.get('root_url'):
        print('add-source needs at least canonical_org=.. and root_url=..'); sys.exit(1)
    if 'tier' in row and row['tier'] is not None:
        row['tier'] = int(row['tier'])
    if 'crawl_cadence_days' in row:
        row['crawl_cadence_days'] = int(row['crawl_cadence_days'])
    conn = db.connect()
    try:
        db.init_schema(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM sieve.source_registry WHERE source_id=%s", (source_id,))
            if cur.fetchone():
                print(f'{source_id!r} already exists — use set-source to edit it'); sys.exit(1)
        # force=True path is the INSERT (insert-only default would still insert a
        # new id; force just also syncs on conflict, which we've excluded above).
        db.upsert_source(conn, row, force=True)
        print(f'added {source_id} (adapter={row["adapter_type"]}, tier={row.get("tier",3)}, '
              f'enabled) — run `probe-source {source_id}` to pre-flight it, then it '
              f'crawls on the next cycle.')
    finally:
        conn.close()


def _toggle(source_id: str, enabled: bool):
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE sieve.source_registry SET enabled=%s "
                        "WHERE source_id=%s RETURNING source_id", (enabled, source_id))
            if not cur.fetchone():
                print(f'no such source {source_id!r}'); sys.exit(1)
        print(f'{source_id}: enabled={enabled}')
    finally:
        conn.close()


def _probe_source(source_id: str):
    """Dry-run pre-flight: fetch the change surface, apply hygiene filters, and
    show what a real cycle WOULD process. Zero DB writes, zero LLM calls —
    mandatory before committing a registry fix (Moz/Ahrefs shipped unverified
    sitemap URLs once and 404'd for weeks)."""
    import httpx
    from . import freshness
    conn = db.connect()
    try:
        from psycopg2.extras import RealDictCursor
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM sieve.source_registry WHERE source_id=%s",
                        (source_id,))
            s = cur.fetchone()
        if not s:
            print(f'no such source {source_id!r}'); sys.exit(1)
        s = dict(s)
        timeout = httpx.Timeout(45, connect=15.0)
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            if s['adapter_type'] == 'sitemap':
                locs = freshness._sitemap_urls(client, s.get('sitemap_url') or '')
                if not locs:
                    print(f'FAIL: sitemap {s.get("sitemap_url")!r} yielded 0 URLs')
                    sys.exit(2)
                kept, seen = [], set()
                for raw, lm in locs:
                    u = freshness.normalize_url(raw)
                    if u not in seen and freshness.url_allowed(s, u):
                        seen.add(u); kept.append(u)
                print(f'OK: sitemap yields {len(locs)} URLs, {len(kept)} pass '
                      f'hygiene+url_filter. Sample:')
                for u in kept[:5]:
                    print('  ', u)
            elif s['adapter_type'] == 'url_list':
                urls = s.get('seed_urls') or []
                ok = 0
                for u in urls:
                    r = client.get(u, headers=freshness.UA)
                    txt = freshness._main_text(r.text) if r.status_code == 200 else ''
                    state = f'{r.status_code}, {len(txt)} chars text'
                    ok += bool(r.status_code == 200 and len(txt) > 200)
                    print(f'  {u} → {state}')
                print(f'OK: {ok}/{len(urls)} seed pages fetch with real text'
                      if ok else 'FAIL: no seed page returned usable text')
                if not ok:
                    sys.exit(2)
            elif s['adapter_type'] == 'github_release':
                cus = freshness._detect_github_release(conn, s, client)
                print(f'OK: adapter reachable; pending release change: '
                      f'{cus[0].new_hash if cus else "none (marker current)"}')
            else:
                print(f'no probe for adapter {s["adapter_type"]!r}')
    finally:
        conn.close()


def _scorecard():
    """Weekly data-quality scorecard — one screen answering 'is the loop healthy
    and is the data it writes any good'. Read-only."""
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT run_id, started_at::date, status, urls_changed, "
                        "objects_written FROM sieve.ingest_runs ORDER BY run_id DESC LIMIT 5")
            print('RUNS (last 5):')
            for r in cur.fetchall():
                print(f'  #{r[0]} {r[1]} [{r[2]}] urls={r[3]} rules+={r[4]}')
            cur.execute("""SELECT extract_status, count(*) FROM sieve.ingest_changes
                           WHERE detected_at >= now()-interval '7 days'
                           GROUP BY 1 ORDER BY 2 DESC""")
            rows = cur.fetchall()
            total = sum(n for _, n in rows) or 1
            print('\nCHANGES last 7d (spend hygiene):')
            for st, n in rows:
                print(f'  {st:11s} {n:4d}  ({100*n//total}%)')
            cur.execute("""SELECT source_id, consecutive_failures, left(coalesce(last_error,''),50)
                           FROM sieve.source_registry WHERE consecutive_failures >= 3""")
            bad = cur.fetchall()
            print(f'\nALERTING STREAKS (>=3 fails): {len(bad) or "none"}')
            for sid, cf, err in bad:
                print(f'  !! {sid} fails={cf} {err}')
            cur.execute("""SELECT source_org, count(*),
                                  count(*) FILTER (WHERE source_url IS NOT NULL AND source_url<>'')
                           FROM sieve.rules WHERE extracted_at >= now()-interval '7 days'
                           GROUP BY 1 ORDER BY 2 DESC LIMIT 8""")
            print('\nRULES ADDED last 7d (org | count | with_url):')
            for org, n, wu in cur.fetchall():
                print(f'  {str(org):22s} {n:4d} {wu:4d}')
            try:
                cur.execute("SELECT count(*), max(last_verified)::date, "
                            "count(*) FILTER (WHERE embedding IS NOT NULL) FROM sieve.rules")
                n, mv, emb = cur.fetchone()
                print(f'\nCORPUS: {n} rules | verified through {mv} | embedded {emb} '
                      f'({100*emb//max(n,1)}%)')
            except Exception:  # no embedding column (pre-vector DB)
                cur.execute("SELECT count(*), max(last_verified)::date FROM sieve.rules")
                n, mv = cur.fetchone()
                print(f'\nCORPUS: {n} rules | verified through {mv} | embeddings n/a')
    finally:
        conn.close()


def _health():
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT source_id, enabled, consecutive_failures,
                       last_ok_at::date, last_crawled_at::date,
                       COALESCE(left(last_error, 60), '')
                FROM sieve.source_registry
                ORDER BY consecutive_failures DESC, source_id""")
            print('SOURCE HEALTH (failures desc):')
            for sid, en, cf, ok_at, cr_at, err in cur.fetchall():
                flag = '!!' if cf >= 3 else ('  ' if en else ' x')
                print(f' {flag} {sid:24s} fails={cf} last_ok={ok_at} '
                      f'last_crawl={cr_at} {err}')
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
    elif cmd == 'migrate-url-state':
        _migrate_url_state()
    elif cmd == 'health':
        _health()
    elif cmd == 'brain-health':
        # Brain-level coverage/drift views (distinct from per-source ops health).
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
    elif cmd == 'scorecard':
        _scorecard()
    elif cmd == 'add-source' and len(sys.argv) > 3:
        _add_source(sys.argv[2], sys.argv[3:])
    elif cmd == 'set-source' and len(sys.argv) > 3:
        _set_source(sys.argv[2], sys.argv[3:])
    elif cmd == 'enable' and len(sys.argv) > 2:
        _toggle(sys.argv[2], True)
    elif cmd == 'disable' and len(sys.argv) > 2:
        _toggle(sys.argv[2], False)
    elif cmd == 'probe-source' and len(sys.argv) > 2:
        _probe_source(sys.argv[2])
    else:
        print(__doc__); sys.exit(1)


if __name__ == '__main__':
    main()
