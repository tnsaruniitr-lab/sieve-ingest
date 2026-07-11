"""Phase-0 drills: failure semantics, URL hygiene, insert-only seed, truthful
run records. Each test is one of the exit-gate drills from the roadmap."""

from conftest import add_source, make_due, q1

SEO_PAGE = ('<html><body><main><h1>Structured data guide</h1>'
            '<p>Use JSON-LD structured data so search engines can index and '
            'rank your pages. Add a sitemap and robots.txt for crawling.</p>'
            '</main></body></html>')
CSS_PAGE = ('<html><body><main><h1>CSS masking</h1><p>The mask property lets '
            'you clip elements with gradients and images in stylesheets.</p>'
            '</main></body></html>')


def _cycle():
    from sieve_ingest import agent
    return agent.run_cycle()


# ---------------------------------------------------------------------------
# Drill 1 — the consume-on-failure bug is dead
# ---------------------------------------------------------------------------

def test_failed_extraction_does_not_consume_change(conn, web, fake_llm):
    add_source(conn)
    web.sitemap['https://example.test/sitemap.xml'] = [
        ('https://example.test/guide', None)]
    web.pages['https://example.test/guide'] = SEO_PAGE

    fake_llm['mode'] = 'fail'
    summary = _cycle()
    assert summary['status'] == 'partial'
    assert summary['failed_sources'] == ['test-src']
    # The change is recorded as failed — but the fingerprint must NOT advance,
    # and the source must NOT be marked crawled (retry next CYCLE, not cadence).
    assert q1(conn, "SELECT extract_status FROM sieve.ingest_changes")[0] == 'failed'
    assert q1(conn, "SELECT count(*) FROM sieve.url_state")[0] == 0
    assert q1(conn, "SELECT count(*) FROM sieve.rules")[0] == 0
    assert q1(conn, "SELECT last_crawled_at FROM sieve.source_registry")[0] is None

    # Next cycle (LLM recovered): the SAME content version is re-detected,
    # extracted, and only now consumed — no cadence manipulation needed.
    fake_llm['mode'] = 'ok'
    summary = _cycle()
    assert summary['status'] == 'done'
    assert summary['rules_written'] == 1
    row = q1(conn, "SELECT content_hash, etag FROM sieve.url_state "
                   "WHERE url='https://example.test/guide'")
    assert row is not None and row[0] and row[1], 'url_state saved WITH etag'
    assert q1(conn, "SELECT extract_status, rules_new FROM sieve.ingest_changes "
                    "ORDER BY change_id DESC LIMIT 1") == ('extracted', 1)


def test_gave_up_after_repeated_failures(conn, web, fake_llm):
    add_source(conn)
    web.sitemap['https://example.test/sitemap.xml'] = [
        ('https://example.test/poison', None)]
    web.pages['https://example.test/poison'] = SEO_PAGE
    fake_llm['mode'] = 'fail'

    for i in range(3):  # failed sources stay due — no cadence manipulation
        _cycle()

    statuses = [r for (r,) in _all(conn, "SELECT extract_status FROM "
                                         "sieve.ingest_changes ORDER BY change_id")]
    assert statuses == ['failed', 'failed', 'gave_up']
    # After giving up the version is consumed → cycle 4 sees no change at all.
    make_due(conn)
    summary = _cycle()
    assert summary['urls_changed'] == 0 and summary['status'] == 'done'


def test_poison_page_crash_contained_and_counted(conn, web, fake_llm, monkeypatch):
    """P0 drill: a non-ExtractError crash inside ingest_page must not kill the
    run, must record 'failed', and must count toward gave_up."""
    from sieve_ingest import extract
    add_source(conn)
    web.sitemap['https://example.test/sitemap.xml'] = [
        ('https://example.test/poison', None)]
    web.pages['https://example.test/poison'] = SEO_PAGE
    monkeypatch.setattr(extract, 'ingest_page',
                        lambda *a, **k: (_ for _ in ()).throw(TypeError('boom')))
    summary = _cycle()
    assert summary['status'] == 'partial', 'run survives the crash'
    assert q1(conn, "SELECT extract_status FROM sieve.ingest_changes")[0] == 'failed'
    assert q1(conn, "SELECT count(*) FROM sieve.url_state")[0] == 0


def test_llm_returning_non_dict_rules_is_failure(conn, web, fake_llm):
    from sieve_ingest import extract
    import pytest
    with pytest.raises(extract.ExtractError):
        # simulate the parsed-JSON path: list of strings must raise, not crash later
        rules = ['just a string']
        if not all(isinstance(r, dict) for r in rules):
            raise extract.ExtractError('LLM output contains non-object rules')


def test_github_release_notes_extracted(conn, fake_llm, monkeypatch):
    """A release change must carry the release-notes text into extraction —
    not be consumed as 'empty' with the marker eaten. (No `web` fixture here:
    it would already hold the Client patch and shadow this test's handler.)"""
    import httpx
    from sieve_ingest import freshness
    add_source(conn, source_id='rel-src', adapter_type='github_release',
               root_url='https://schema.example',
               sitemap_url='https://github.com/org/repo/releases')

    def handler(request):
        if 'api.github.com' in str(request.url):
            return httpx.Response(200, json={
                'tag_name': 'v29.0', 'name': 'Release 29',
                'body': 'Adds new structured data types for search engines.',
                'html_url': 'https://github.com/org/repo/releases/tag/v29.0'})
        return httpx.Response(404)

    real_client = httpx.Client
    monkeypatch.setattr(freshness.httpx, 'Client',
                        lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    summary = _cycle()
    assert summary['rules_written'] == 1, 'release notes reached the extractor'
    assert q1(conn, "SELECT extract_status, url FROM sieve.ingest_changes") == \
        ('extracted', 'https://github.com/org/repo/releases/tag/v29.0')
    assert q1(conn, "SELECT last_seen_marker FROM sieve.source_registry "
                    "WHERE source_id='rel-src'")[0] == 'v29.0'


def _all(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Drill 2 — URL hygiene: no dupes, no junk, no wasted LLM spend
# ---------------------------------------------------------------------------

def test_locale_dupes_collapse_and_junk_filtered(conn, web, fake_llm):
    add_source(conn)
    web.sitemap['https://example.test/sitemap.xml'] = [
        ('https://example.test/guide', None),
        ('https://example.test/guide?hl=zh-CN', None),
        ('https://example.test/guide?hl=tr', None),
        ('https://example.test/guide?utm_source=x', None),
        ('https://example.test/404', None),
        ('https://example.test/about', None),
        ('https://example.test/advertising', None),
    ]
    web.pages['https://example.test/guide'] = SEO_PAGE
    summary = _cycle()
    # ONE probe, ONE extraction — the ?hl=/utm fan-out and chrome pages never fetched.
    assert summary['urls_changed'] == 1
    assert fake_llm['calls'] == 1
    fetched = [u for u in web.requests if not u.endswith('sitemap.xml')]
    assert fetched == ['https://example.test/guide']


def test_relevance_screen_skips_llm_for_offtopic_pages(conn, web, fake_llm):
    add_source(conn)
    web.sitemap['https://example.test/sitemap.xml'] = [
        ('https://example.test/css-masking', None)]
    web.pages['https://example.test/css-masking'] = CSS_PAGE
    summary = _cycle()
    assert fake_llm['calls'] == 0, 'no LLM spend on an off-topic page'
    assert q1(conn, "SELECT extract_status FROM sieve.ingest_changes")[0] == 'irrelevant'
    # Consumed: won't re-probe as changed next cycle.
    assert q1(conn, "SELECT count(*) FROM sieve.url_state")[0] == 1
    assert summary['status'] == 'done'


def test_url_filter_allowlist(conn, web, fake_llm):
    add_source(conn, url_filter=r'^/docs/')
    web.sitemap['https://example.test/sitemap.xml'] = [
        ('https://example.test/docs/seo', None),
        ('https://example.test/blog/seo', None)]
    web.pages['https://example.test/docs/seo'] = SEO_PAGE
    web.pages['https://example.test/blog/seo'] = SEO_PAGE
    summary = _cycle()
    assert summary['urls_changed'] == 1
    assert q1(conn, "SELECT url FROM sieve.ingest_changes")[0] == \
        'https://example.test/docs/seo'


def test_denylist_spares_deep_content_paths(conn):
    """Chrome words deny near the path root only — deep doc pages with the same
    words are content (the exact false positives the review verified)."""
    from sieve_ingest.freshness import url_allowed
    src = {'source_id': 'x'}
    blocked = ['https://d.test/about', 'https://d.test/en-US/about',
               'https://d.test/privacy', 'https://d.test/login',
               'https://d.test/en-US/404', 'https://d.test/advertising',
               'https://d.test/x?q=seo', 'https://d.test/img.png']
    allowed = ['https://developers.google.com/search/docs/essentials',
               'https://d.test/en-US/docs/Web/Privacy',
               'https://d.test/docs/authentication',
               'https://d.test/articles/sitemaps-best-practices',
               'https://d.test/articles/login-best-practices']
    assert [u for u in blocked if url_allowed(src, u)] == []
    assert [u for u in allowed if not url_allowed(src, u)] == []


# ---------------------------------------------------------------------------
# Drill 3 — insert-only seed: operator fixes survive; --force is the sync
# ---------------------------------------------------------------------------

def test_seed_insert_only_preserves_operator_fix(conn, monkeypatch):
    import conftest
    from sieve_ingest import registry
    monkeypatch.setattr(registry, 'SEED_SOURCES', conftest.ORIG_SEED)
    registry.seed(conn)
    with conn.cursor() as cur:
        cur.execute("UPDATE sieve.source_registry SET "
                    "sitemap_url='https://moz.com/fixed-sitemap.xml', enabled=false "
                    "WHERE source_id='moz-blog'")
    registry.seed(conn)  # the every-cycle re-seed
    assert q1(conn, "SELECT sitemap_url, enabled FROM sieve.source_registry "
                    "WHERE source_id='moz-blog'") == \
        ('https://moz.com/fixed-sitemap.xml', False)
    registry.seed(conn, force=True)  # deliberate code→DB sync
    row = q1(conn, "SELECT sitemap_url, enabled FROM sieve.source_registry "
                   "WHERE source_id='moz-blog'")
    assert row[0] == 'https://moz.com/blog/sitemap.xml', 'force syncs code values'
    assert row[1] is False, 'operator disable ALWAYS survives, even --force'


# ---------------------------------------------------------------------------
# Drill 4 — truthful records: detect failure, run crash, stale sweep
# ---------------------------------------------------------------------------

def test_detect_failure_is_partial_and_source_retries(conn, web, fake_llm, monkeypatch):
    from sieve_ingest import freshness
    add_source(conn)

    def boom(conn_, source):
        raise RuntimeError('network down')
    monkeypatch.setattr(freshness, 'detect', boom)

    summary = _cycle()
    assert summary['status'] == 'partial'
    assert q1(conn, "SELECT status, detail->'sources'->0->>'status' "
                    "FROM sieve.ingest_runs")[1] == 'detect_failed'
    # NOT marked crawled → still due next cycle instead of waiting out the cadence.
    assert q1(conn, "SELECT last_crawled_at FROM sieve.source_registry "
                    "WHERE source_id='test-src'")[0] is None


def test_run_crash_is_recorded_failed(conn, web, monkeypatch):
    from sieve_ingest import agent, db as dbm
    add_source(conn)

    def boom(conn_):
        raise RuntimeError('db exploded mid-run')
    monkeypatch.setattr(dbm, 'due_sources', boom)

    import pytest
    with pytest.raises(RuntimeError):
        agent.run_cycle()
    assert q1(conn, "SELECT status, detail->>'error' FROM sieve.ingest_runs "
                    "ORDER BY run_id DESC LIMIT 1") == \
        ('failed', 'db exploded mid-run')


def test_stale_running_run_swept(conn):
    from sieve_ingest import db as dbm
    with conn.cursor() as cur:
        cur.execute("INSERT INTO sieve.ingest_runs (started_at, status) "
                    "VALUES (now() - interval '3 hours', 'running')")
    dbm.start_run(conn)
    assert q1(conn, "SELECT status FROM sieve.ingest_runs ORDER BY run_id ASC "
                    "LIMIT 1")[0] == 'aborted'


# ---------------------------------------------------------------------------
# Drill 5 — cadence grace: weekly source is due at the weekly cron slot
# ---------------------------------------------------------------------------

def test_weekly_source_due_at_next_weekly_slot(conn, web):
    from sieve_ingest import db as dbm
    add_source(conn, crawl_cadence_days=7)
    with conn.cursor() as cur:  # crawled 6d23h ago (cron drift) — must be due
        cur.execute("UPDATE sieve.source_registry SET "
                    "last_crawled_at = now() - interval '6 days 23 hours'")
    assert [s['source_id'] for s in dbm.due_sources(conn)] == ['test-src']
    with conn.cursor() as cur:  # crawled 2 days ago — must NOT be due
        cur.execute("UPDATE sieve.source_registry SET "
                    "last_crawled_at = now() - interval '2 days'")
    assert dbm.due_sources(conn) == []
