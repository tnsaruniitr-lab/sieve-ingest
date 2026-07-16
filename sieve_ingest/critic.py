"""
critic.py — completeness critic: verify the brain covers the AEO/SEO canon.

`python -m sieve_ingest critic` seeds sieve.canon_topics (~45 canonical topics
an AEO/SEO auditor must have rules for), probes sieve.rules for each, records
results in sieve.canon_probe_results, and prints failures. A topic FAILS when
it has fewer than min_hits rules at confidence >= min_conf.

Probing uses ILIKE alternation over name+if_condition+then_logic — the same
text fields the auditor's FTS retrieval indexes, so a probe miss here means
the auditor would come up empty too. Deterministic; judgment about WHETHER a
gap matters stays with the validate-audit-ruleset skill, which reads
canon_probe_results instead of recomputing.
"""

from __future__ import annotations

import logging

from . import db

log = logging.getLogger('ingest.critic')

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sieve.canon_topics (
    topic      text PRIMARY KEY,
    domain_tag text NOT NULL DEFAULT 'general',
    patterns   text[] NOT NULL,          -- ILIKE alternatives, any-match
    min_hits   int NOT NULL DEFAULT 3,
    min_conf   numeric NOT NULL DEFAULT 0.7
);
CREATE TABLE IF NOT EXISTS sieve.canon_probe_results (
    topic        text NOT NULL,
    run_at       timestamptz NOT NULL DEFAULT now(),
    hits         int NOT NULL,
    hi_conf_hits int NOT NULL,
    fresh_hits   int NOT NULL,           -- created/verified in last 90d
    ok           boolean NOT NULL
);
ALTER TABLE sieve.canon_probe_results
    ADD COLUMN IF NOT EXISTS observed_hits int NOT NULL DEFAULT 0;
"""

# (topic, domain_tag, patterns, min_hits, min_conf)
TOPICS = [
    # --- AI crawler control (the AEO Discovery gate) ---
    ('gptbot',            'aeo', ['%gptbot%', '%oai-searchbot%', '%chatgpt-user%'], 3, 0.7),
    ('perplexitybot',     'aeo', ['%perplexitybot%', '%perplexity-user%'], 3, 0.7),
    ('claudebot',         'aeo', ['%claudebot%', '%claude-searchbot%', '%claude-user%', '%anthropic%crawl%'], 3, 0.7),
    ('google-extended',   'aeo', ['%google-extended%'], 3, 0.7),
    ('robots-txt',        'seo', ['%robots.txt%'], 10, 0.7),
    ('llms-txt',          'aeo', ['%llms.txt%', '%llms-txt%'], 3, 0.7),
    ('ai-overviews',      'aeo', ['%ai overview%', '%ai mode%', '%sge%generative%'], 10, 0.7),
    ('crawler-verification', 'seo', ['%verify%googlebot%', '%reverse dns%crawl%', '%user agent%verif%'], 2, 0.6),
    # --- Structured data ---
    ('json-ld',           'seo', ['%json-ld%', '%jsonld%'], 10, 0.7),
    ('faq-schema',        'seo', ['%faqpage%', '%faq schema%', '%faq structured%'], 5, 0.7),
    ('article-schema',    'seo', ['%article%schema%', '%schema%article%', '%newsarticle%'], 5, 0.7),
    ('organization-schema', 'entity', ['%organization%schema%', '%schema%organization%'], 3, 0.7),
    ('product-schema',    'seo', ['%product%schema%', '%schema%product%', '%merchant%listing%'], 3, 0.7),
    ('howto-schema',      'seo', ['%howto%', '%how-to schema%'], 2, 0.6),
    ('breadcrumb-schema', 'seo', ['%breadcrumb%'], 3, 0.7),
    ('review-schema',     'seo', ['%review snippet%', '%aggregaterating%', '%review%schema%'], 3, 0.7),
    ('speakable',         'aeo', ['%speakable%'], 1, 0.5),
    # --- Entity / trust ---
    ('same-as',           'entity', ['%sameas%', '%same-as%'], 3, 0.7),
    ('knowledge-graph',   'entity', ['%knowledge graph%', '%knowledge panel%'], 3, 0.7),
    ('eeat',              'content', ['%e-e-a-t%', '%eeat%', '%experience, expertise%', '%rater%guideline%'], 5, 0.7),
    ('author-credentials', 'content', ['%hascredential%', '%author%credential%', '%author%bio%', '%author%expert%'], 5, 0.7),
    ('ymyl',              'content', ['%ymyl%', '%your money%your life%'], 3, 0.7),
    ('about-page-trust',  'entity', ['%about page%', '%about us%trust%', '%contact%trust%'], 2, 0.6),
    # --- Classic technical SEO ---
    ('canonical-tag',     'seo', ['%canonical%'], 10, 0.7),
    ('title-tag',         'seo', ['%title tag%', '%title link%', '%page title%'], 8, 0.7),
    ('meta-description',  'seo', ['%meta description%'], 5, 0.7),
    ('hreflang',          'seo', ['%hreflang%'], 5, 0.7),
    ('sitemap-xml',       'seo', ['%sitemap%'], 8, 0.7),
    ('internal-linking',  'seo', ['%internal link%'], 5, 0.7),
    ('redirects',         'seo', ['%301%', '%redirect%'], 5, 0.7),
    ('noindex',           'seo', ['%noindex%', '%meta robots%'], 5, 0.7),
    ('javascript-seo',    'seo', ['%javascript%render%', '%csr%seo%', '%client-side render%'], 3, 0.7),
    ('mobile-first',      'seo', ['%mobile-first%', '%mobile friendly%', '%responsive%index%'], 3, 0.7),
    # --- Performance ---
    ('core-web-vitals',   'performance', ['%core web vitals%', '%lcp%', '%inp%', '% cls%', '%largest contentful%'], 8, 0.7),
    ('page-speed',        'performance', ['%page speed%', '%load time%', '%ttfb%'], 5, 0.7),
    # --- AEO answer optimization ---
    ('answer-format',     'aeo', ['%direct answer%', '%answer-first%', '%answer first%', '%concise answer%'], 5, 0.7),
    ('question-headings', 'aeo', ['%question%heading%', '%h2%question%', '%people also ask%'], 3, 0.7),
    ('chunk-retrieval',   'aeo', ['%chunk%', '%passage%retriev%', '%extractab%'], 3, 0.6),
    ('citations-ai',      'geo', ['%cited by ai%', '%ai citation%', '%llm%cit%', '%citation%chatgpt%', '%citation%perplexity%'], 5, 0.6),
    ('wikipedia-presence', 'geo', ['%wikipedia%'], 3, 0.6),
    ('reddit-presence',   'geo', ['%reddit%'], 3, 0.6),
    ('freshness-dates',   'content', ['%datemodified%', '%date modified%', '%freshness%', '%last updated%'], 5, 0.7),
    ('content-depth',     'content', ['%comprehensive%cover%', '%topical%depth%', '%topic cluster%'], 3, 0.6),
    ('duplicate-content', 'content', ['%duplicate content%'], 3, 0.7),
    ('paywall-cloaking',  'seo', ['%cloak%', '%paywall%structured%'], 2, 0.6),
    # --- Measured gaps from the 2026-07-16 answermonk value test (expected to
    # FAIL until the enrichment loop closes them — that is the point) ---
    ('ai-share-of-voice', 'geo', ['%share of voice%', '%brand%absent%ai%', '%brand%mention%ai answer%', '%ai visibility gap%'], 3, 0.85),
    ('b2b-review-platforms', 'geo', ['%g2%review%', '%capterra%', '%trustpilot%', '%software review platform%'], 3, 0.7),
    ('statistics-sourcing', 'content', ['%statistic%with%source%', '%cite%statistic%', '%data%citation%source%'], 3, 0.85),
    ('orphan-pages',      'seo', ['%orphan page%'], 2, 0.7),
    ('snippet-displacement', 'aeo', ['%featured snippet%displac%', '%win%featured snippet%', '%snippet%competitor%'], 3, 0.85),
]

PROBE_SQL = """
INSERT INTO sieve.canon_probe_results (topic, hits, hi_conf_hits, fresh_hits, observed_hits, ok)
SELECT t.topic,
       count(r.id) AS hits,
       count(r.id) FILTER (WHERE r.confidence_score ~ '^[0-9]+(\.[0-9]+)?$'
             AND r.confidence_score::numeric >= t.min_conf) AS hi_conf_hits,
       count(r.id) FILTER (WHERE coalesce(r.last_verified, r.extracted_at)
             > now() - interval '90 days') AS fresh_hits,
       count(r.id) FILTER (WHERE r.rule_type = 'observed') AS observed_hits,
       count(r.id) FILTER (WHERE r.confidence_score ~ '^[0-9]+(\.[0-9]+)?$'
             AND r.confidence_score::numeric >= t.min_conf) >= t.min_hits AS ok
FROM sieve.canon_topics t
LEFT JOIN sieve.rules r
  ON r.status IS DISTINCT FROM 'deprecated'
 AND (r.name||' '||coalesce(r.if_condition,'')||' '||coalesce(r.then_logic,''))
     ILIKE ANY (t.patterns)
GROUP BY t.topic, t.min_hits
RETURNING topic, hits, hi_conf_hits, fresh_hits, observed_hits, ok
"""


def seed_topics(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
        for topic, tag, patterns, min_hits, min_conf in TOPICS:
            cur.execute("""
                INSERT INTO sieve.canon_topics (topic, domain_tag, patterns, min_hits, min_conf)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (topic) DO UPDATE SET domain_tag=EXCLUDED.domain_tag,
                    patterns=EXCLUDED.patterns, min_hits=EXCLUDED.min_hits,
                    min_conf=EXCLUDED.min_conf
            """, (topic, tag, patterns, min_hits, min_conf))
    return len(TOPICS)


def run_probe(conn=None) -> dict:
    own = conn is None
    conn = conn or db.connect()
    try:
        n = seed_topics(conn)
        with conn.cursor() as cur:
            cur.execute(PROBE_SQL)
            rows = cur.fetchall()
        rows.sort(key=lambda r: (r[5], r[1]))  # failures first, thinnest first
        failed = [r for r in rows if not r[5]]
        print(f'=== CANON PROBE — {n} topics, {len(failed)} FAILING ===')
        for topic, hits, hi, fresh, observed, ok in rows:
            mark = 'ok ' if ok else 'FAIL'
            obs = f' observed={observed}' if observed else ''
            print(f'  [{mark}] {topic:22s} hits={hits:<4d} hi_conf={hi:<4d} fresh_90d={fresh}{obs}')
        if failed:
            print('\n  → failing topics need new sources or targeted extraction; '
                  'see sieve.canon_probe_results history for trend.')
        return {'topics': n, 'failing': [r[0] for r in failed]}
    finally:
        if own:
            conn.close()
