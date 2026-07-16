"""
harvest_pages.py — catch-up crawl WITHOUT LLM extraction (file-bridge mode).

Runs the freshness detection for ALL enabled sources (ignoring cadence — this
is the one-time catch-up), fetches changed pages, and dumps their text to a
JSONL file for in-chat extraction by local Claude. NO Anthropic API calls, and
NO state writes (url_state / last_crawled / ingest_runs untouched) — state is
committed later by ingest_extracted.py only for pages whose rules were written,
preserving the retry-on-failure semantics.

    railway run .venv-local/bin/python harvest_pages.py <out.jsonl> [max_per_source]
"""
import json
import sys

from sieve_ingest import db, freshness

MAX_PER_SOURCE = int(sys.argv[2]) if len(sys.argv) > 2 else 25
OUT = sys.argv[1] if len(sys.argv) > 1 else 'harvest.jsonl'


def main():
    conn = db.connect()
    from psycopg2.extras import RealDictCursor
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM sieve.source_registry WHERE enabled ORDER BY tier, source_id")
        sources = [dict(r) for r in cur.fetchall()]
    print(f'{len(sources)} enabled sources', flush=True)

    n_pages = 0
    with open(OUT, 'w', encoding='utf-8') as f:
        for s in sources:
            try:
                changes = freshness.detect(conn, s)
            except Exception as e:
                print(f'  {s["source_id"]}: detect failed: {e}', flush=True)
                continue
            kept = 0
            for ch in changes:
                if kept >= MAX_PER_SOURCE:
                    break
                text = ch.text
                if not text:
                    from sieve_ingest.extract import _fetch_text
                    text = _fetch_text(ch.url)
                if not text or len(text.strip()) < 200:
                    continue
                f.write(json.dumps({
                    'source_id': s['source_id'], 'org': s['canonical_org'],
                    'url': ch.url, 'title': getattr(ch, 'title', '') or '',
                    'change_type': ch.change_type, 'signal': ch.signal,
                    'new_hash': ch.new_hash,
                    'text': text[:12000],
                }, ensure_ascii=False) + '\n')
                kept += 1
                n_pages += 1
            print(f'  {s["source_id"]}: {len(changes)} changed, {kept} harvested', flush=True)
    print(f'\nDONE — {n_pages} pages harvested to {OUT}', flush=True)
    conn.close()


if __name__ == '__main__':
    main()
