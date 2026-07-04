"""
enrich_exact_urls.py — exhaustively ingest every EXACT doc page across the
url_list sources (Google, Perplexity, OpenAI, Bing) so their rules carry precise
per-page source URLs instead of a generic hub.

SAFE: only inserts/refreshes rules (upsert_rule dedupes by rule_key + refreshes
last_verified — it NEVER deletes). Existing rules are preserved; new precise-URL
rules coexist. Resumable (url_state fingerprints skip unchanged pages next run).

Prints per-page progress so status can be relayed live.
"""
import os
import sys
import time
from sieve_ingest import db, registry, freshness, extract
from psycopg2.extras import RealDictCursor


def coverage(cur):
    cur.execute("""
        SELECT count(*) FILTER (WHERE source_url IS NOT NULL AND source_url<>'') AS withurl,
               count(*) AS total
        FROM sieve.rules
        WHERE source_org IN ('Google','Perplexity','OpenAI','Bing')
    """)
    r = cur.fetchone()
    return r['withurl'], r['total']


def main():
    conn = db.connect()
    db.init_schema(conn)
    registry.seed(conn)
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("SELECT * FROM sieve.source_registry "
                "WHERE adapter_type='url_list' AND enabled ORDER BY tier, source_id")
    sources = [dict(r) for r in cur.fetchall()]
    print(f"START — {len(sources)} url_list sources: "
          f"{[s['source_id'] for s in sources]}", flush=True)
    w0, t0 = coverage(cur)
    print(f"before: {w0}/{t0} Google/Perplexity/OpenAI/Bing rules have a URL", flush=True)

    total_new = total_refreshed = 0
    for s in sources:
        try:
            changes = freshness.detect(conn, s)
        except Exception as e:
            print(f"[{s['source_id']}] detect failed: {e}", flush=True)
            continue
        print(f"\n[{s['source_id']}] {len(changes)} pages to ingest", flush=True)
        for i, ch in enumerate(changes):
            try:
                counts = extract.ingest_page(conn, ch, s)
                db.save_url_state(conn, ch.url, s['source_id'], None, None, ch.new_hash)
            except Exception as e:
                print(f"  [{s['source_id']}] {i+1}/{len(changes)} FAILED {ch.url}: {e}", flush=True)
                continue
            total_new += counts.get('new', 0)
            total_refreshed += counts.get('refreshed', 0)
            tail = ch.url.split('/search/docs/')[-1] if '/search/docs/' in ch.url else ch.url.split('/')[-1]
            print(f"  [{s['source_id']}] {i+1}/{len(changes)} {tail[:44]:44s} "
                  f"+{counts.get('new',0)} new / {counts.get('refreshed',0)} refreshed "
                  f"(run total +{total_new})", flush=True)
        db.mark_source_crawled(conn, s['source_id'], None)

    w1, t1 = coverage(cur)
    print(f"\nDONE — {total_new} new, {total_refreshed} refreshed exact-URL rules", flush=True)
    print(f"after: {w1}/{t1} Google/Perplexity/OpenAI/Bing rules have a URL "
          f"(+{w1-w0} with-URL rules)", flush=True)
    conn.close()


if __name__ == '__main__':
    main()
