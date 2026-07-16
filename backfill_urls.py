"""
backfill_urls.py — give URL-less/generic brain objects an EXACT source URL.

    python backfill_urls.py [--table rules|principles|anti_patterns]

rules (default): for each authoritative-source rule whose URL is MISSING or
GENERIC (hub-level, < 4 path slashes), find its nearest high-similarity
neighbor (same source_org) with a MORE specific URL, and adopt it.

principles / anti_patterns: these tables have ~0% direct URLs, so neighbors
come from sieve.RULES of the same source_org — the rules corpus carries the
URL knowledge. Adoption threshold applies identically.

DATA-SAFE:
  * candidate filter — NEVER touches rows whose url_provenance records an
    exact/doc-join method, and never rows that already have a specific URL;
  * writes a dated backup table (id, source_url, document_id, url_provenance)
    before mutating;
  * does NOT set last_verified (a neighbor guess is not verification) and
    does NOT adopt document_id cross-table (that would fabricate lineage);
  * objects with no confident neighbor are left exactly as they are.
Every adopted URL is recorded in url_provenance ({method:'neighbor', at})
so citation trust is inspectable and re-runs skip already-adopted rows only
when they gained specificity.

Runs server-side (LATERAL) in id-batches so it's bounded, resumable, and shows
progress. Requires embeddings (run embed_brain.py first for new rows).
"""
import json
import os
import sys
from datetime import date, datetime, timezone

import psycopg2

DB_URL = os.getenv('SIEVE_DB_URL') or os.getenv('DATABASE_URL')
THRESHOLD = float(os.getenv('BACKFILL_SIM', '0.80'))
BATCH = int(os.getenv('BACKFILL_BATCH', '1500'))
# hub-level cutoff: fewer than this many '/' in the URL = generic
GENERIC_SPEC = int(os.getenv('BACKFILL_GENERIC_SPEC', '4'))
AUTHORITATIVE = ('Google', 'Perplexity', 'OpenAI', 'Anthropic', 'Bing', 'W3C', 'MDN',
                 'web.dev', 'Backlinko', 'Ahrefs', 'Semrush', 'Moz', 'Schema.org',
                 'Search Engine Land', 'Search Engine Journal', 'Search Engine Roundtable',
                 'Growth Memo', 'AI Citation Research', 'llmstxt.org')

# slashes = specificity proxy; NULL/'' => -1 (least specific)
SPEC = "CASE WHEN {c} IS NULL OR {c}='' THEN -1 " \
       "ELSE (length({c})-length(replace({c},'/',''))) END"

# never overwrite provenance the ingest/doc-join established as authoritative
# (%% because these strings go through psycopg2 parameterized execute)
PROTECT = ("(src.url_provenance IS NULL OR src.url_provenance::text NOT SIMILAR TO "
           "'%%(exact|exact-upgrade|doc-join)%%')")

VALID_TABLES = ('rules', 'principles', 'anti_patterns')


def main():
    table = 'rules'
    if '--table' in sys.argv:
        table = sys.argv[sys.argv.index('--table') + 1]
    if table not in VALID_TABLES:
        sys.exit(f'--table must be one of {VALID_TABLES}')
    if not DB_URL:
        sys.exit('SIEVE_DB_URL / DATABASE_URL not set')
    neighbor_table = 'rules'
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = True
    cur = conn.cursor()

    # --- dated backup of everything this run may mutate ---
    stamp = date.today().strftime('%Y%m%d')
    bak = f'sieve.bak_backfill_{table}_{stamp}'
    cur.execute(f"SELECT to_regclass('{bak}')")
    if cur.fetchone()[0] is None:
        cur.execute(f"""CREATE TABLE {bak} AS
            SELECT id, source_url, document_id, url_provenance
            FROM sieve.{table} WHERE source_org = ANY(%s)""", (list(AUTHORITATIVE),))
        print(f'backup {bak} written ({cur.rowcount} rows)', flush=True)

    # candidates: authoritative + embedded + missing-or-generic URL + not exact-provenance
    cand_where = f"""source_org = ANY(%s) AND embedding IS NOT NULL
              AND ({SPEC.format(c='source_url')}) < {GENERIC_SPEC}
              AND (url_provenance IS NULL OR url_provenance::text NOT SIMILAR TO
                   '%%(exact|exact-upgrade|doc-join)%%')"""
    cur.execute(f"SELECT count(*) FROM sieve.{table} WHERE {cand_where}",
                (list(AUTHORITATIVE),))
    todo = cur.fetchone()[0]
    print(f"[{table}] candidates (URL-less/generic, non-exact provenance): {todo:,} "
          f"(sim>={THRESHOLD}, neighbors from sieve.{neighbor_table})", flush=True)

    prov = json.dumps({'method': 'neighbor', 'note': f'sim>={THRESHOLD} from {neighbor_table}',
                       'at': datetime.now(timezone.utc).isoformat(timespec='seconds')})
    # cross-table adoption must not fabricate document lineage
    set_doc = ("document_id = COALESCE(nn.document_id, t.document_id),"
               if table == neighbor_table else "")

    upgraded = 0
    last_id = ''
    while True:
        cur.execute(f"""
            SELECT id FROM sieve.{table}
            WHERE {cand_where} AND id > %s
            ORDER BY id LIMIT {BATCH}
        """, (list(AUTHORITATIVE), last_id))
        ids = [r[0] for r in cur.fetchall()]
        if not ids:
            break
        last_id = ids[-1]
        cur.execute(f"""
            UPDATE sieve.{table} t
            SET source_url = nn.source_url,
                {set_doc}
                url_provenance = %s
            FROM sieve.{table} src
            CROSS JOIN LATERAL (
                SELECT r2.source_url, r2.document_id
                FROM sieve.{neighbor_table} r2
                WHERE r2.source_org = src.source_org
                  AND r2.embedding IS NOT NULL
                  AND r2.source_url IS NOT NULL AND r2.source_url <> ''
                  AND ({SPEC.format(c='r2.source_url')}) > ({SPEC.format(c='src.source_url')})
                  AND (1 - (r2.embedding <=> src.embedding)) >= {THRESHOLD}
                ORDER BY r2.embedding <=> src.embedding
                LIMIT 1
            ) nn
            WHERE t.id = src.id AND src.id = ANY(%s) AND {PROTECT}
        """, (prov, ids))
        upgraded += cur.rowcount
        print(f"  batch up to id {last_id}: +{cur.rowcount} upgraded (total {upgraded:,})", flush=True)

    cur.execute(f"""
        SELECT count(*) FILTER (WHERE {SPEC.format(c='source_url')} >= {GENERIC_SPEC}) AS specific,
               count(*) FILTER (WHERE source_url IS NOT NULL AND source_url<>'') AS anyurl,
               count(*) AS total
        FROM sieve.{table} WHERE source_org = ANY(%s)
    """, (list(AUTHORITATIVE),))
    spec, anyurl, total = cur.fetchone()
    print(f"\n[{table}] DONE — upgraded {upgraded:,} objects.", flush=True)
    print(f"authoritative objects: {total:,} | with any URL: {anyurl:,} "
          f"({100*anyurl//max(total,1)}%) | with SPECIFIC URL: {spec:,} "
          f"({100*spec//max(total,1)}%)", flush=True)
    conn.close()


if __name__ == '__main__':
    main()
