"""
registry.py — the governed Source Registry seed.

The canonical sources the ingestion agent polls, with authority tier, the change
signal to watch (adapter_type), and cadence. Tier-1 = first-party primary docs
(crawled weekly); Tier-2 = respected secondary (monthly). This is the single
control surface — add a source = add a row, no code change.
"""

from __future__ import annotations

from . import db

# adapter_type: how we detect change for this source
#   sitemap        — diff <lastmod> in sitemap.xml (default)
#   github_release — watch a GitHub repo's latest release tag (cleanest for Schema.org)
#   changelog      — watch a documented "what's new" page (Google)
SEED_SOURCES = [
    # ---- Tier 1: first-party primary (weekly) ----
    dict(source_id='google-search-central', canonical_org='Google', tier=1,
         adapter_type='changelog', crawl_cadence_days=7,
         root_url='https://developers.google.com/search',
         sitemap_url='https://developers.google.com/sitemap.xml',
         notes='SEO/AEO primary. Watch the documentation changelog.'),
    dict(source_id='schema-org', canonical_org='Schema.org', tier=1,
         adapter_type='github_release', crawl_cadence_days=7,
         root_url='https://schema.org',
         sitemap_url='https://github.com/schemaorg/schemaorg/releases',
         notes='Version-released on GitHub — watch the release tag, not HTML.'),
    dict(source_id='bing-webmaster', canonical_org='Bing', tier=1,
         adapter_type='sitemap', crawl_cadence_days=14,
         root_url='https://www.bing.com/webmasters/help',
         sitemap_url=None, notes='Bing/IndexNow guidance.'),
    dict(source_id='web-dev', canonical_org='web.dev', tier=1,
         adapter_type='sitemap', crawl_cadence_days=14,
         root_url='https://web.dev',
         sitemap_url='https://web.dev/sitemap.xml', notes='Core Web Vitals / performance.'),
    dict(source_id='w3c', canonical_org='W3C', tier=1,
         adapter_type='sitemap', crawl_cadence_days=30,
         root_url='https://www.w3.org/TR/', sitemap_url=None),
    dict(source_id='mdn', canonical_org='MDN', tier=1,
         adapter_type='sitemap', crawl_cadence_days=30,
         root_url='https://developer.mozilla.org',
         sitemap_url='https://developer.mozilla.org/sitemap.xml'),
    dict(source_id='perplexity-docs', canonical_org='Perplexity', tier=1,
         adapter_type='sitemap', crawl_cadence_days=14,
         root_url='https://docs.perplexity.ai', sitemap_url=None,
         notes='AEO answer-engine guidance.'),
    dict(source_id='openai-docs', canonical_org='OpenAI', tier=1,
         adapter_type='sitemap', crawl_cadence_days=14,
         root_url='https://platform.openai.com/docs', sitemap_url=None,
         notes='GPTBot / SearchGPT crawler guidance.'),
    # ---- Tier 2: respected secondary (monthly) ----
    dict(source_id='backlinko', canonical_org='Backlinko', tier=2,
         adapter_type='sitemap', crawl_cadence_days=30,
         root_url='https://backlinko.com', sitemap_url='https://backlinko.com/sitemap.xml'),
    dict(source_id='ahrefs-blog', canonical_org='Ahrefs', tier=2,
         adapter_type='sitemap', crawl_cadence_days=30,
         root_url='https://ahrefs.com/blog', sitemap_url='https://ahrefs.com/blog/sitemap.xml'),
    dict(source_id='semrush-blog', canonical_org='Semrush', tier=2,
         adapter_type='sitemap', crawl_cadence_days=30,
         root_url='https://www.semrush.com/blog', sitemap_url=None),
    dict(source_id='moz-blog', canonical_org='Moz', tier=2,
         adapter_type='sitemap', crawl_cadence_days=30,
         root_url='https://moz.com/blog', sitemap_url='https://moz.com/blog/sitemap.xml'),
    dict(source_id='search-engine-land', canonical_org='Search Engine Land', tier=3,
         adapter_type='sitemap', crawl_cadence_days=30,
         root_url='https://searchengineland.com', sitemap_url=None),
]


def seed(conn=None) -> int:
    own = conn is None
    conn = conn or db.connect()
    try:
        db.init_schema(conn)
        for s in SEED_SOURCES:
            db.upsert_source(conn, s)
        return len(SEED_SOURCES)
    finally:
        if own:
            conn.close()
