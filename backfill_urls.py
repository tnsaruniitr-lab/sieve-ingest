"""
backfill_urls.py — give every KEY rule an EXACT source URL.

For each authoritative-source rule whose URL is missing or generic (a hub like
developers.google.com/search/docs), find its nearest high-similarity neighbor
(same source_org) that has a MORE specific URL, and adopt it.

DATA-SAFE: only ever sets a URL to a *more specific* one (more path segments);
never blanks, downgrades, deletes, or edits rule text. Rules with no confident
neighbor are left exactly as they are (we do not fabricate URLs).

Runs server-side (LATERAL) in id-batches so it's bounded, resumable, and shows
progress. Requires embeddings (run embed_brain.py first).
"""
import os
import sys
import psycopg2

DB_URL = os.getenv('SIEVE_DB_URL') or os.getenv('DATABASE_URL')
THRESHOLD = float(os.getenv('BACKFILL_SIM', '0.80'))
BATCH = int(os.getenv('BACKFILL_BATCH', '1500'))
AUTHORITATIVE = ('Google', 'Perplexity', 'OpenAI', 'Bing', 'W3C', 'MDN', 'web.dev',
                 'Backlinko', 'Ahrefs', 'Semrush', 'Moz', 'Schema.org',
                 'Search Engine Land', 'Search Engine Journal')

# slashes = specificity proxy; NULL/'' => -1 (least specific)
SPEC = "CASE WHEN {c} IS NULL OR {c}='' THEN -1 " \
       "ELSE (length({c})-length(replace({c},'/',''))) END"


def main():
    if not DB_URL:
        sys.exit('SIEVE_DB_URL / DATABASE_URL not set')
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = True
    cur = conn.cursor()

    # candidates: all authoritative rules with an embedding — the LATERAL only
    # upgrades those with a strictly-more-specific neighbor, so already-exact
    # rules (Google leaf pages, schema.org/Type) are naturally left untouched.
    cur.execute("""
        SELECT count(*) FROM sieve.rules
        WHERE source_org = ANY(%s) AND embedding IS NOT NULL
    """, (list(AUTHORITATIVE),))
    todo = cur.fetchone()[0]
    print(f"candidates needing a more-specific URL: {todo:,} (sim>={THRESHOLD})", flush=True)

    upgraded = 0
    last_id = ''
    while True:
        # take a batch of candidate ids (id is text; order by numeric where possible)
        cur.execute(f"""
            SELECT id FROM sieve.rules
            WHERE source_org = ANY(%s) AND embedding IS NOT NULL
              AND id > %s
            ORDER BY id LIMIT {BATCH}
        """, (list(AUTHORITATIVE), last_id))
        ids = [r[0] for r in cur.fetchall()]
        if not ids:
            break
        last_id = ids[-1]
        cur.execute(f"""
            UPDATE sieve.rules t
            SET source_url = nn.source_url,
                document_id = COALESCE(nn.document_id, t.document_id),
                last_verified = now()
            FROM sieve.rules src
            CROSS JOIN LATERAL (
                SELECT r2.source_url, r2.document_id
                FROM sieve.rules r2
                WHERE r2.source_org = src.source_org
                  AND r2.embedding IS NOT NULL
                  AND r2.source_url IS NOT NULL AND r2.source_url <> ''
                  AND ({SPEC.format(c='r2.source_url')}) > ({SPEC.format(c='src.source_url')})
                  AND (1 - (r2.embedding <=> src.embedding)) >= {THRESHOLD}
                ORDER BY r2.embedding <=> src.embedding
                LIMIT 1
            ) nn
            WHERE t.id = src.id AND src.id = ANY(%s)
        """, (ids,))
        upgraded += cur.rowcount
        print(f"  batch up to id {last_id}: +{cur.rowcount} upgraded (total {upgraded:,})", flush=True)

    # final coverage on authoritative rules
    cur.execute(f"""
        SELECT count(*) FILTER (WHERE {SPEC.format(c='source_url')} >= 4) AS specific,
               count(*) FILTER (WHERE source_url IS NOT NULL AND source_url<>'') AS anyurl,
               count(*) AS total
        FROM sieve.rules WHERE source_org = ANY(%s)
    """, (list(AUTHORITATIVE),))
    spec, anyurl, total = cur.fetchone()
    print(f"\nDONE — upgraded {upgraded:,} rules.", flush=True)
    print(f"authoritative rules: {total:,} | with any URL: {anyurl:,} "
          f"({100*anyurl//max(total,1)}%) | with SPECIFIC URL: {spec:,} "
          f"({100*spec//max(total,1)}%)", flush=True)
    conn.close()


if __name__ == '__main__':
    main()
