"""Phase-2 drills: rotation cursor, retry-first probing, alerting, scorecard."""

import os
import subprocess
import sys

from conftest import add_source, q1

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SEO_PAGE = ('<html><body><main><p>Structured data and sitemaps help search '
            'engines crawl, index and rank pages. {}</p></main></body></html>')


def _cycle():
    from sieve_ingest import agent
    return agent.run_cycle()


def _index_site(web, n_children=3, urls_per_child=2):
    """Sitemap index with n children, each holding SEO pages."""
    idx = 'https://example.test/sitemap.xml'
    children = []
    body = ('<?xml version="1.0"?><sitemapindex '
            'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{}</sitemapindex>')
    locs = ''
    for c in range(n_children):
        child = f'https://example.test/sitemap-{c}.xml'
        children.append(child)
        locs += f'<sitemap><loc>{child}</loc></sitemap>'
        web.sitemap[child] = []
        for u in range(urls_per_child):
            page = f'https://example.test/c{c}/page{u}'
            web.sitemap[child].append((page, None))
            web.pages[page] = SEO_PAGE.format(f'child {c} page {u}')
    web.pages[idx] = body.format(locs)  # served as raw page (index XML)
    return children


def test_cursor_rotates_across_index_children(conn, web, fake_llm):
    from sieve_ingest import db as dbm
    add_source(conn)
    _index_site(web, n_children=3)
    _cycle()
    assert dbm.get_crawl_cursor(conn, 'test-src') == {'child': 1}
    with conn.cursor() as cur:
        cur.execute("UPDATE sieve.source_registry SET last_crawled_at=NULL")
    _cycle()
    assert dbm.get_crawl_cursor(conn, 'test-src') == {'child': 2}
    with conn.cursor() as cur:
        cur.execute("UPDATE sieve.source_registry SET last_crawled_at=NULL")
    _cycle()
    assert dbm.get_crawl_cursor(conn, 'test-src') == {'child': 0}, 'wraps'


def test_budget_limited_walk_covers_all_children_over_cycles(conn, web, fake_llm, monkeypatch):
    """With a probe budget smaller than one cycle's URLs, successive cycles must
    still reach every child (the audit's 'later pages structurally invisible')."""
    from sieve_ingest import freshness
    add_source(conn)
    _index_site(web, n_children=4, urls_per_child=3)
    orig = freshness._detect_sitemap
    monkeypatch.setattr(freshness, '_detect_sitemap',
                        lambda conn_, s, c, max_fetch=20: orig(conn_, s, c, max_fetch=3))
    for _ in range(4):
        _cycle()
        with conn.cursor() as cur:
            cur.execute("UPDATE sieve.source_registry SET last_crawled_at=NULL")
    seen = {u for (u,) in _all(conn, "SELECT url FROM sieve.url_state")}
    children_hit = {u.split('/')[3] for u in seen if '/c' in u}
    assert children_hit == {'c0', 'c1', 'c2', 'c3'}, f'all children reached: {children_hit}'


def test_retry_first_beats_cursor_rotation(conn, web, fake_llm):
    """A failed page must be re-probed next cycle even though the cursor has
    moved to a different child."""
    add_source(conn)
    _index_site(web, n_children=3)
    fake_llm['mode'] = 'fail'
    _cycle()  # child 0 pages fail, cursor moves to child 1
    failed = [u for (u,) in _all(conn, "SELECT url FROM sieve.ingest_changes "
                                       "WHERE extract_status='failed'")]
    assert failed, 'precondition: something failed'
    fake_llm['mode'] = 'ok'
    _cycle()  # failed sources stay due; retry-first must pick c0 pages up
    for u in failed:
        assert q1(conn, "SELECT extract_status FROM sieve.ingest_changes "
                        "WHERE url=%s ORDER BY change_id DESC LIMIT 1", u)[0] == 'extracted'


def _all(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def test_deadman_ping_and_alert_webhook(conn, web, fake_llm, monkeypatch):
    from sieve_ingest import agent
    calls = {'get': [], 'post': []}

    class FakeHttpx:
        @staticmethod
        def get(url, timeout=None):
            calls['get'].append(url)

        @staticmethod
        def post(url, timeout=None, json=None):
            calls['post'].append((url, json))

    monkeypatch.setattr(agent, 'HEALTHCHECK_PING_URL', 'https://hc.test/ping')
    monkeypatch.setattr(agent, 'ALERT_WEBHOOK_URL', 'https://alerts.test/hook')
    monkeypatch.setattr('httpx.get', FakeHttpx.get)
    monkeypatch.setattr('httpx.post', FakeHttpx.post)

    add_source(conn)
    web.sitemap['https://example.test/sitemap.xml'] = [
        ('https://example.test/guide', None)]
    web.pages['https://example.test/guide'] = SEO_PAGE.format('x')
    _cycle()
    assert calls['get'] == ['https://hc.test/ping'], 'clean run pings success URL'
    assert calls['post'] == [], 'no alert on a clean run'

    fake_llm['mode'] = 'fail'
    web.pages['https://example.test/guide'] = SEO_PAGE.format('changed!')
    with conn.cursor() as cur:
        cur.execute("UPDATE sieve.source_registry SET last_crawled_at=NULL")
    _cycle()
    assert calls['get'][-1] == 'https://hc.test/ping/fail', 'partial run pings /fail'
    assert calls['post'] and calls['post'][-1][1]['summary']['status'] == 'partial'


def test_scorecard_runs(conn, web, fake_llm):
    add_source(conn)
    web.sitemap['https://example.test/sitemap.xml'] = [
        ('https://example.test/guide', None)]
    web.pages['https://example.test/guide'] = SEO_PAGE.format('x')
    _cycle()
    r = subprocess.run([sys.executable, '-m', 'sieve_ingest', 'scorecard'],
                       capture_output=True, text=True, cwd=REPO_ROOT)
    assert r.returncode == 0, r.stderr
    assert 'CORPUS:' in r.stdout and 'CHANGES last 7d' in r.stdout
