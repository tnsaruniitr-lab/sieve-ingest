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
             # 2025-26 AI-search layer (added 2026-07-16 — the load-bearing AEO docs)
             'https://developers.google.com/search/docs/fundamentals/ai-optimization-guide',
             'https://developers.google.com/search/docs/appearance/ranking-systems-guide',
             'https://developers.google.com/search/docs/essentials/spam-policies',
             'https://developers.google.com/search/docs/crawling-indexing/google-common-crawlers',
         ],
         notes='SEO/AEO primary — exact doc pages (url_list) so each rule cites its precise page.'),
    dict(source_id='google-search-updates', canonical_org='Google', tier=1,
         adapter_type='changelog', crawl_cadence_days=7,
         root_url='https://developers.google.com/search/updates',
         notes='First-party news-of-record: Search Central documentation changelog. '
               'Changed hash → re-ingest; catches new docs/policies ~30d before tier-2 blogs.'),
    dict(source_id='anthropic-docs', canonical_org='Anthropic', tier=1,
         adapter_type='url_list', crawl_cadence_days=14,
         root_url='https://support.claude.com',
         seed_urls=[
             'https://support.claude.com/en/articles/8896518-does-anthropic-crawl-data-from-the-web-and-how-can-site-owners-block-the-crawler',
             'https://privacy.claude.com/en/articles/8896518-does-anthropic-crawl-data-from-the-web-and-how-can-site-owners-block-the-crawler',
         ],
         notes='ClaudeBot / Claude-User / Claude-SearchBot crawler semantics (updated 2026-02). '
               'First-party grounding for Claude-visibility rules.'),
    dict(source_id='google-qrg', canonical_org='Google', tier=1,
         adapter_type='url_list', crawl_cadence_days=90,
         root_url='https://guidelines.raterhub.com',
         seed_urls=[
             'https://services.google.com/fh/files/misc/hsw-sqrg.pdf',
             'https://guidelines.raterhub.com/searchqualityevaluatorguidelines.pdf',
         ],
         notes='Search Quality Rater Guidelines (E-E-A-T primary source, Sept-2025 rev). PDF.'),
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
         sitemap_url='https://web.dev/sitemap.xml',
         url_filter=r'^/(articles|learn|blog)/',
         notes='Core Web Vitals / performance. url_filter keeps the crawl on '
               'content paths (the Jul-6 run burned budget on locale dupes).'),
    dict(source_id='w3c', canonical_org='W3C', tier=1,
         adapter_type='url_list', crawl_cadence_days=30,
         root_url='https://www.w3.org/TR/',
         seed_urls=[
             'https://www.w3.org/TR/WCAG22/',
             'https://www.w3.org/TR/wai-aria-1.2/',
             'https://www.w3.org/TR/appmanifest/',
         ],
         notes='w3.org has NO sitemap (probed 2026-07-11: /sitemap.xml and '
               '/TR/sitemap.xml both 404) — url_list of the SEO/AEO-adjacent '
               'specs instead.'),
    dict(source_id='mdn', canonical_org='MDN', tier=1,
         adapter_type='sitemap', crawl_cadence_days=30,
         root_url='https://developer.mozilla.org',
         sitemap_url='https://developer.mozilla.org/sitemap.xml',
         url_filter=r'^/en-US/docs/(Web/(HTML|HTTP|Performance|Accessibility|Media)|Glossary)\b',
         notes='url_filter limits the 14k-page docs tree to SEO-relevant trees '
               '(the Jul-11 backfill extracted game-dev "rules" at tier-1) and '
               'blocks chrome/locales.'),
    dict(source_id='perplexity-docs', canonical_org='Perplexity', tier=1,
         adapter_type='url_list', crawl_cadence_days=14,
         root_url='https://docs.perplexity.ai',
         seed_urls=[
             # Fixed 2026-07-16: crawler/publisher docs, not API onboarding pages.
             'https://docs.perplexity.ai/guides/bots',
             'https://www.perplexity.ai/perplexitybot',
         ],
         notes='PerplexityBot / Perplexity-User crawler + publisher guidance — exact pages.'),
    dict(source_id='openai-docs', canonical_org='OpenAI', tier=1,
         adapter_type='url_list', crawl_cadence_days=14,
         root_url='https://developers.openai.com',
         seed_urls=[
             # Docs migrated to developers.openai.com (2026); old platform URLs kept
             # as redirect fallbacks (follow_redirects=True).
             'https://developers.openai.com/api/docs/bots',
             'https://platform.openai.com/docs/bots',
             'https://platform.openai.com/docs/gptbot',
         ],
         notes='GPTBot / OAI-SearchBot / ChatGPT-User crawler guidance — exact pages.'),
    # ---- Tier 2: respected secondary (monthly) ----
    dict(source_id='backlinko', canonical_org='Backlinko', tier=2,
         adapter_type='sitemap', crawl_cadence_days=30,
         root_url='https://backlinko.com', sitemap_url='https://backlinko.com/sitemap.xml',
         url_filter=r'seo|keyword|link|serp|rank|search|google|backlink|content'
                    r'|speed|schema|snippet|traffic|crawl',
         notes='flat-slug blog; allowlist keeps SEO posts, drops the email/'
               'marketing/tool-review long tail (Jul-11: sales-copy, clubhouse, '
               'email-open-rate each produced 8 "rules").'),
    dict(source_id='ahrefs-blog', canonical_org='Ahrefs', tier=2,
         adapter_type='sitemap', crawl_cadence_days=30,
         root_url='https://ahrefs.com/blog',
         sitemap_url='https://ahrefs.com/blog/post-sitemap.xml',
         notes='post-sitemap.xml is the direct urlset (probed 200, 2026-07-11); '
               'the old /blog/sitemap.xml 404s.'),
    dict(source_id='semrush-blog', canonical_org='Semrush', tier=2,
         adapter_type='sitemap', crawl_cadence_days=30,
         root_url='https://www.semrush.com/blog',
         sitemap_url='https://www.semrush.com/sitemap.xml',
         url_filter=r'^/blog/',
         notes='no blog-only sitemap exists (probed 2026-07-11) — root index + '
               'url_filter keeps the crawl on /blog/.'),
    dict(source_id='moz-blog', canonical_org='Moz', tier=2,
         adapter_type='sitemap', crawl_cadence_days=30,
         root_url='https://moz.com/blog',
         sitemap_url='https://moz.com/blog-sitemap.xml',
         notes='blog-sitemap.xml is the direct urlset (probed 200, 2026-07-11); '
               'the old /blog/sitemap.xml 404s.'),
    dict(source_id='search-engine-land', canonical_org='Search Engine Land', tier=3,
         adapter_type='sitemap', crawl_cadence_days=30,
         root_url='https://searchengineland.com',
         sitemap_url='https://searchengineland.com/sitemap_index.xml',
         notes='sitemap_index.xml probed 200 2026-07-11; site rate-limits (429) — '
               'the 20-probe cap keeps us polite.'),
    # ---- Added 2026-07-16: AI-search / GEO evidence layer ----
    dict(source_id='ai-citation-studies', canonical_org='AI Citation Research', tier=2,
         adapter_type='url_list', crawl_cadence_days=30,
         root_url='https://otterly.ai',
         seed_urls=[
             'https://www.tryprofound.com/blog/ai-platform-citation-patterns',
             'https://otterly.ai/blog/the-ai-citations-report-2026/',
         ],
         notes='Large-N "what do AI engines actually cite" studies → GEO presence rules.'),
    dict(source_id='growth-memo', canonical_org='Growth Memo', tier=2,
         adapter_type='url_list', crawl_cadence_days=30,
         root_url='https://www.growth-memo.com',
         seed_urls=[
             'https://www.growth-memo.com/p/state-of-ai-search-optimization-2026',
         ],
         notes='Kevin Indig — highest-rigor practitioner AEO research (flagship posts).'),
    dict(source_id='llms-txt', canonical_org='llmstxt.org', tier=3,
         adapter_type='url_list', crawl_cadence_days=90,
         root_url='https://llmstxt.org',
         seed_urls=['https://llmstxt.org/'],
         notes='llms.txt spec — NEGATIVE-evidence source: adoption ~9%, AI crawlers '
               'rarely fetch it, Google will not support it. Keeps the auditor honest.'),
    dict(source_id='search-engine-journal', canonical_org='Search Engine Journal', tier=3,
         adapter_type='sitemap', crawl_cadence_days=30,
         root_url='https://www.searchenginejournal.com',
         sitemap_url='https://www.searchenginejournal.com/sitemap_index.xml',
         notes='High-volume AEO/GEO reporting (AI-search guide coverage, bot-doc changes).'),
    dict(source_id='seroundtable', canonical_org='Search Engine Roundtable', tier=3,
         adapter_type='sitemap', crawl_cadence_days=14,
         root_url='https://www.seroundtable.com',
         sitemap_url='https://www.seroundtable.com/sitemap.xml',
         notes='Day-zero record of algorithm/crawler-doc changes — freshness signal.'),
]


def seed(conn=None, force: bool = False) -> int:
    """Insert-only by default: fills missing sources, never touches existing rows
    (operator DB fixes survive the every-cycle re-seed). `force=True` is the
    deliberate code→DB sync (run `python -m sieve_ingest seed --force` once after
    changing SEED_SOURCES); it still never overwrites `enabled`."""
    own = conn is None
    conn = conn or db.connect()
    try:
        db.init_schema(conn)
        for s in SEED_SOURCES:
            db.upsert_source(conn, s, force=force)
        return len(SEED_SOURCES)
    finally:
        if own:
            conn.close()
