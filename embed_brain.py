"""
embed_brain.py — backfill pgvector embeddings for brain objects that lack them.

    python embed_brain.py            # all three tables, WHERE embedding IS NULL
    python embed_brain.py --dry-run  # counts only

New rules from the ingest cron arrive WITHOUT embeddings (extract.py skips the
vector pass by design); this script closes that gap so vector retrieval and
the neighbor-URL backfill keep covering new rows. Idempotent — re-run anytime.

Requires OPENAI_API_KEY (existing brain vectors are 1536-dim; verified against
pg_attribute before writing — aborts if the column dim is not 1536).
Uses raw httpx against the embeddings endpoint; no openai package needed.
"""
import os
import sys

import httpx
import psycopg2

DB_URL = os.getenv('SIEVE_DB_URL') or os.getenv('DATABASE_URL')
OPENAI_KEY = os.getenv('OPENAI_API_KEY')
MODEL = os.getenv('EMBED_MODEL', 'text-embedding-3-small')  # 1536-dim
BATCH = int(os.getenv('EMBED_BATCH', '96'))

# table → SQL expression producing the text to embed
TABLES = {
    'rules': "coalesce(name,'')||' '||coalesce(if_condition,'')||' '||coalesce(then_logic,'')",
    'principles': "coalesce(title,'')||' '||coalesce(statement,'')||' '||coalesce(explanation,'')",
    'anti_patterns': "coalesce(title,'')||' '||coalesce(description,'')",
}


def _embed(client: httpx.Client, texts):
    r = client.post('https://api.openai.com/v1/embeddings',
                    headers={'Authorization': f'Bearer {OPENAI_KEY}'},
                    json={'model': MODEL, 'input': texts},
                    timeout=60.0)
    r.raise_for_status()
    data = r.json()['data']
    return [d['embedding'] for d in sorted(data, key=lambda d: d['index'])]


def main():
    dry = '--dry-run' in sys.argv
    if not DB_URL:
        sys.exit('SIEVE_DB_URL / DATABASE_URL not set')
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = True
    cur = conn.cursor()

    # dim guard — never write mismatched vectors
    cur.execute("""SELECT atttypmod FROM pg_attribute
                   WHERE attrelid='sieve.rules'::regclass AND attname='embedding'""")
    dim = cur.fetchone()[0]
    if dim != 1536:
        sys.exit(f'embedding column dim={dim}, expected 1536 — wrong model pairing, aborting')

    total_todo = 0
    for tbl in TABLES:
        cur.execute(f"SELECT count(*) FROM sieve.{tbl} WHERE embedding IS NULL")
        n = cur.fetchone()[0]
        total_todo += n
        print(f'{tbl}: {n} rows missing embedding')
    if dry or total_todo == 0:
        conn.close()
        return

    if not OPENAI_KEY:
        sys.exit('OPENAI_API_KEY not set — cannot embed')

    with httpx.Client() as client:
        for tbl, text_expr in TABLES.items():
            done = 0
            while True:
                cur.execute(f"""
                    SELECT id, left({text_expr}, 6000) FROM sieve.{tbl}
                    WHERE embedding IS NULL ORDER BY id LIMIT {BATCH}
                """)
                rows = cur.fetchall()
                if not rows:
                    break
                ids = [r[0] for r in rows]
                texts = [(r[1] or ' ').strip() or ' ' for r in rows]
                try:
                    vecs = _embed(client, texts)
                except Exception as e:
                    sys.exit(f'{tbl}: embedding call failed after {done} rows: {e}')
                for _id, vec in zip(ids, vecs):
                    cur.execute(
                        f"UPDATE sieve.{tbl} SET embedding=%s::vector WHERE id=%s",
                        ('[' + ','.join(f'{x:.7f}' for x in vec) + ']', _id))
                done += len(rows)
                print(f'  {tbl}: embedded {done} rows', flush=True)
            print(f'{tbl}: DONE ({done} embedded)')
    conn.close()


if __name__ == '__main__':
    main()
