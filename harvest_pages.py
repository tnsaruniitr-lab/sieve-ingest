"""
harvest_pages.py — catch-up crawl WITHOUT LLM extraction (file-bridge mode).

Runs the freshness detection for ALL enabled sources (ignoring cadence — this
is the one-time catch-up), fetches changed pages, and dumps their text to a
JSONL file for in-chat extraction by local Claude. NO Anthropic API calls, and
NO change-consuming state writes (changed pages' url_state / last_crawled /
ingest_runs untouched) — state is committed later by ingest_extracted.py only
for pages whose rules were written, preserving the retry-on-failure semantics.
(UNCHANGED pages do get their url_state refreshed and their citing rules'
last_verified re-stamped inside freshness.detect — that observation is real
whichever caller makes it.)

    railway run .venv-local/bin/python harvest_pages.py <out.jsonl> [max_per_source]
"""
import json
import sys

from sieve_ingest import db, freshness

MAX_PER_SOURCE = int(sys.argv[2]) if len(sys.argv) > 2 else 25
OUT = sys.argv[1] if len(sys.argv) > 1 else 'harvest.jsonl'


def _fetch_text(url: str) -> str:
    """On-demand fetch for changes that signal without carrying text (the old
    extract._fetch_text — extract.py no longer fetches, so the bridge owns it).
    Best effort; empty string on failure."""
    try:
        import httpx

        from sieve_ingest.freshness import UA, _main_text
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            r = client.get(url, headers=UA)
            if r.status_code == 200:
                return _main_text(r.text)
    except Exception as e:
        print(f'  on-demand fetch failed for {url}: {e}', flush=True)
    return ''


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
                text = ch.text or _fetch_text(ch.url)
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
            print(f'  {s["source_id"]}: {len(changes)} changed, {kept} harvested, '
                  f'{freshness.detect_stats["urls_unchanged"]} unchanged '
                  f'({freshness.detect_stats["verified_refreshed"]} rules re-verified)',
                  flush=True)
    print(f'\nDONE — {n_pages} pages harvested to {OUT}', flush=True)
    conn.close()


if __name__ == '__main__':
    main()
