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
         adapter_type='url_list', crawl_cadence_days=7,
         root_url='https://developers.google.com/search',
         sitemap_url='https://developers.google.com/sitemap.xml',
         seed_urls=[
             'https://developers.google.com/search/docs/essentials',
             'https://developers.google.com/search/docs/fundamentals/seo-starter-guide',
             'https://developers.google.com/search/docs/fundamentals/creating-helpful-content',
             'https://developers.google.com/search/docs/appearance/ai-features',
             'https://developers.google.com/search/docs/appearance/structured-data/intro-structured-data',
             'https://developers.google.com/search/docs/appearance/structured-data/article',
             'https://developers.google.com/search/docs/appearance/structured-data/faqpage',
             'https://developers.google.com/search/docs/appearance/structured-data/breadcrumb',
             'https://developers.google.com/search/docs/appearance/structured-data/product',
             'https://developers.google.com/search/docs/appearance/structured-data/local-business',
             'https://developers.google.com/search/docs/appearance/structured-data/organization',
             'https://developers.google.com/search/docs/appearance/structured-data/review-snippet',
             'https://developers.google.com/search/docs/appearance/title-link',
             'https://developers.google.com/search/docs/appearance/snippet',
             'https://developers.google.com/search/docs/crawling-indexing/sitemaps/overview',
             'https://developers.google.com/search/docs/crawling-indexing/robots/intro',
             'https://developers.google.com/search/docs/crawling-indexing/canonicalization',
             'https://developers.google.com/search/docs/crawling-indexing/javascript/javascript-seo-basics',
         ],
         notes='SEO/AEO primary — exact doc pages (url_list) so each rule cites its precise page.'),
    dict(source_id='schema-org', canonical_org='Schema.org', tier=1,
         adapter_type='github_release', crawl_cadence_days=7,
         root_url='https://schema.org',
         sitemap_url='https://github.com/schemaorg/schemaorg/releases',
         notes='Version-released on GitHub — watch the release tag, not HTML.'),
    dict(source_id='bing-webmaster', canonical_org='Bing', tier=1,
         adapter_type='url_list', crawl_cadence_days=14,
         root_url='https://www.bing.com/webmasters/help',
         seed_urls=[
             'https://www.bing.com/webmasters/help/webmaster-guidelines-30fba23a',
             'https://www.bing.com/webmasters/help/sitemaps-3b5cf6ed',
             'https://www.indexnow.org/documentation',
         ],
         notes='Bing/IndexNow guidance — exact pages.'),
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
         adapter_type='url_list', crawl_cadence_days=14,
         root_url='https://docs.perplexity.ai',
         seed_urls=[
             'https://docs.perplexity.ai/home',
             'https://docs.perplexity.ai/guides/getting-started',
         ],
         notes='AEO answer-engine guidance — exact pages.'),
    dict(source_id='openai-docs', canonical_org='OpenAI', tier=1,
         adapter_type='url_list', crawl_cadence_days=14,
         root_url='https://platform.openai.com/docs',
         seed_urls=[
             'https://platform.openai.com/docs/bots',
             'https://platform.openai.com/docs/gptbot',
         ],
         notes='GPTBot / SearchGPT crawler guidance — exact pages.'),
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
