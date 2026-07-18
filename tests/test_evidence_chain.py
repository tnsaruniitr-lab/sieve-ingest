"""Evidence-chain drills (CONTRACT.md section 7, sieve-ingest half):
monitor-mode warts (marker must not advance; skipped changes leave a
'skipped_monitor' trail) + the deprecation path (LLM-marked and screen-flagged
rules land with status='deprecated', and a refresh never reactivates them)."""

import httpx
import pytest

from conftest import add_source, make_due, q1

HOWTO_PAGE = ('<html><body><main><h1>HowTo structured data</h1>'
              '<p>Use HowTo structured data markup so your steps can appear '
              'as HowTo rich results and improve search ranking.</p>'
              '</main></body></html>')


def _cycle():
    from sieve_ingest import agent
    return agent.run_cycle()


def _all(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def _mock_release(monkeypatch, tag='v29.0'):
    from sieve_ingest import freshness

    def handler(request):
        if 'api.github.com' in str(request.url):
            return httpx.Response(200, json={
                'tag_name': tag, 'name': f'Release {tag}',
                'body': 'Adds new structured data types for search engines.',
                'html_url': f'https://github.com/org/repo/releases/tag/{tag}'})
        return httpx.Response(404)

    real_client = httpx.Client
    monkeypatch.setattr(freshness.httpx, 'Client',
                        lambda **kw: real_client(transport=httpx.MockTransport(handler)))


# ---------------------------------------------------------------------------
# Monitor-mode warts (MAX_URLS_PER_SOURCE=0)
# ---------------------------------------------------------------------------

def test_monitor_mode_marker_not_advanced_and_trail(conn, fake_llm, monkeypatch):
    """MAX_URLS_PER_SOURCE=0 must NOT advance the github_release marker (the
    release would be eaten with nothing extracted) and must record each
    detection as 'skipped_monitor' — a per-URL trail across weekly cycles.
    Re-enabling extraction still sees the pending change."""
    from sieve_ingest import agent
    monkeypatch.setattr(agent, 'MAX_URLS_PER_SOURCE', 0)
    add_source(conn, source_id='rel-src', adapter_type='github_release',
               root_url='https://schema.example',
               sitemap_url='https://github.com/org/repo/releases')
    _mock_release(monkeypatch)

    summary = _cycle()
    assert summary['status'] == 'done', 'monitor skips are clean, not partial'
    assert fake_llm['calls'] == 0, 'no LLM spend in monitor mode'
    assert q1(conn, "SELECT last_seen_marker FROM sieve.source_registry "
                    "WHERE source_id='rel-src'")[0] is None, 'marker NOT advanced'
    assert q1(conn, "SELECT last_crawled_at FROM sieve.source_registry "
                    "WHERE source_id='rel-src'")[0] is not None
    assert q1(conn, "SELECT extract_status, url FROM sieve.ingest_changes") == \
        ('skipped_monitor', 'https://github.com/org/repo/releases/tag/v29.0')

    # Cycle 2 (still monitoring): the same release is RE-detected → trail row.
    make_due(conn, 'rel-src')
    _cycle()
    statuses = [r for (r,) in _all(conn, "SELECT extract_status FROM "
                                         "sieve.ingest_changes ORDER BY change_id")]
    assert statuses == ['skipped_monitor', 'skipped_monitor']

    # Extraction re-enabled: the change was never consumed, so it's still there.
    monkeypatch.setattr(agent, 'MAX_URLS_PER_SOURCE', 15)
    make_due(conn, 'rel-src')
    summary = _cycle()
    assert summary['rules_written'] == 1
    assert q1(conn, "SELECT extract_status FROM sieve.ingest_changes "
                    "ORDER BY change_id DESC LIMIT 1")[0] == 'extracted'
    assert q1(conn, "SELECT last_seen_marker FROM sieve.source_registry "
                    "WHERE source_id='rel-src'")[0] == 'v29.0'


def test_monitor_mode_sitemap_consumes_nothing(conn, web, fake_llm, monkeypatch):
    """Monitor mode on a sitemap source: change recorded as skipped_monitor,
    url_state untouched (nothing consumed), no rules written, no LLM calls."""
    from sieve_ingest import agent
    monkeypatch.setattr(agent, 'MAX_URLS_PER_SOURCE', 0)
    add_source(conn)
    web.sitemap['https://example.test/sitemap.xml'] = [
        ('https://example.test/guide', None)]
    web.pages['https://example.test/guide'] = HOWTO_PAGE

    summary = _cycle()
    assert summary['status'] == 'done'
    assert fake_llm['calls'] == 0
    assert q1(conn, "SELECT extract_status FROM sieve.ingest_changes")[0] == \
        'skipped_monitor'
    assert q1(conn, "SELECT count(*) FROM sieve.url_state")[0] == 0
    assert q1(conn, "SELECT count(*) FROM sieve.rules")[0] == 0


# ---------------------------------------------------------------------------
# Deprecation path (source-marked + screen-flagged)
# ---------------------------------------------------------------------------

def test_llm_deprecated_status_reaches_the_brain(conn, web, monkeypatch):
    """A rule the LLM emits with status='deprecated' (source marks it
    deprecated/retired/sunset) must land in sieve.rules as 'deprecated'."""
    from sieve_ingest import extract
    add_source(conn)
    web.sitemap['https://example.test/sitemap.xml'] = [
        ('https://example.test/old', None)]
    web.pages['https://example.test/old'] = HOWTO_PAGE
    monkeypatch.setattr(extract, '_extract_rules', lambda text, org, url: [
        {'name': 'Add author bylines', 'if_condition': 'article pages',
         'then_logic': 'show a byline', 'domain_tag': 'content',
         'confidence_score': 0.9, 'status': 'deprecated'}])

    summary = _cycle()
    assert summary['rules_written'] == 1
    assert q1(conn, "SELECT status FROM sieve.rules")[0] == 'deprecated'


def test_screen_flags_howto_claims_as_deprecated():
    """The quality screen flags 'HowTo rich result' style claims onto the
    deprecated path when the LLM emitted them as active — never silently kept."""
    from sieve_ingest import extract
    rules = [
        {'name': 'Add HowTo markup', 'if_condition': 'step-by-step content',
         'then_logic': 'use HowTo structured data for HowTo rich results',
         'confidence_score': 0.9},
        {'name': 'Use JSON-LD', 'if_condition': 'any page',
         'then_logic': 'prefer JSON-LD structured data',
         'confidence_score': 0.9},
    ]
    kept, rejected = extract._validate_rules(rules, 'https://example.test/x')
    assert rejected == 0 and len(kept) == 2
    assert extract._rule_status(kept[0]) == 'deprecated', 'HowTo claim flagged'
    assert extract._rule_status(kept[1]) == 'active'


def test_refresh_carries_deprecation_and_never_blanks_it(conn):
    """upsert_rule: a re-extraction that says the source now marks the guidance
    deprecated must win over the reactivate-on-refresh default."""
    from sieve_ingest import db
    db.init_schema(conn)
    rule = {'name': 'HowTo markup', 'if_condition': 'step content',
            'then_logic': 'add HowTo schema', 'confidence_score': 0.9}
    doc = db.upsert_document(conn, 'https://example.test/d', 'TestOrg', 't', 'seo')
    assert db.upsert_rule(conn, rule, doc, 'https://example.test/d', 'TestOrg') == 'new'
    assert q1(conn, "SELECT status FROM sieve.rules")[0] == 'active'
    assert db.upsert_rule(conn, rule, doc, 'https://example.test/d', 'TestOrg',
                          status='deprecated') == 'refreshed'
    assert q1(conn, "SELECT status FROM sieve.rules")[0] == 'deprecated'
