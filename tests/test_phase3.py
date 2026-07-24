"""Phase-3 drills: retire/reactivate lifecycle, quality gates, provenance,
long-doc chunking."""

from conftest import add_source, q1

SEO_PAGE = ('<html><body><main><p>Structured data and sitemaps help search '
            'engines crawl, index and rank pages. {}</p></main></body></html>')


def _cycle():
    from sieve_ingest import agent
    return agent.run_cycle()


def test_removed_page_retires_rules_and_resurrection_reactivates(conn, web, fake_llm):
    add_source(conn)
    web.sitemap['https://example.test/sitemap.xml'] = [
        ('https://example.test/guide', None)]
    web.pages['https://example.test/guide'] = SEO_PAGE.format('v1')
    summary = _cycle()
    assert summary['rules_written'] == 1

    # Page vanishes → 404 → rules retired, change recorded, fingerprint dropped.
    del web.pages['https://example.test/guide']
    with conn.cursor() as cur:
        cur.execute("UPDATE sieve.source_registry SET last_crawled_at=NULL")
    summary = _cycle()
    assert summary['rules_retired'] == 1
    assert q1(conn, "SELECT status FROM sieve.rules WHERE source_url="
                    "'https://example.test/guide'")[0] == 'retired'
    assert q1(conn, "SELECT extract_status FROM sieve.ingest_changes "
                    "ORDER BY change_id DESC LIMIT 1")[0] == 'retired'
    assert q1(conn, "SELECT count(*) FROM sieve.url_state")[0] == 0

    # Page comes back with the same content → re-extracted → same rule_key
    # REACTIVATES instead of duplicating.
    web.pages['https://example.test/guide'] = SEO_PAGE.format('v1')
    with conn.cursor() as cur:
        cur.execute("UPDATE sieve.source_registry SET last_crawled_at=NULL")
    _cycle()
    assert q1(conn, "SELECT status, count(*) FROM sieve.rules GROUP BY 1") == \
        ('active', 1), 'one rule, active again — no duplicate'


def test_quality_gate_rejects_low_confidence_and_incomplete(conn, web, fake_llm, monkeypatch):
    from sieve_ingest import extract
    add_source(conn)
    web.sitemap['https://example.test/sitemap.xml'] = [
        ('https://example.test/guide', None)]
    web.pages['https://example.test/guide'] = SEO_PAGE.format('x')

    def sketchy(text, org, url):
        excerpt = 'Structured data and sitemaps help search engines crawl'
        return [
            {'name': 'good', 'if_condition': 'a', 'then_logic': 'b',
             'source_excerpt': excerpt, 'confidence_score': 0.9},
            {'name': 'low-conf', 'if_condition': 'a', 'then_logic': 'b',
             'source_excerpt': excerpt, 'confidence_score': 0.3},
            {'name': 'no-then', 'if_condition': 'a', 'source_excerpt': excerpt,
             'confidence_score': 0.95},
            {'name': 'bad-conf', 'if_condition': 'a', 'then_logic': 'b',
             'source_excerpt': excerpt, 'confidence_score': 'high'},
        ]
    monkeypatch.setattr(extract, '_extract_rules', sketchy)
    summary = _cycle()
    assert summary['rules_written'] == 1, 'only the clean rule lands'
    assert q1(conn, "SELECT name FROM sieve.rules")[0] == 'good'


def test_migration_adds_newer_column_when_older_provenance_exists(conn):
    """Regression: init_schema must add url_provenance even on a table that
    ALREADY has superseded_by (the prod state where gating on one sentinel
    silently skipped the newer column, breaking every rule INSERT)."""
    from sieve_ingest import db as dbm
    with conn.cursor() as cur:
        # Simulate the prod schema: Phase-0 columns present, Phase-3 absent.
        cur.execute("ALTER TABLE sieve.rules DROP COLUMN IF EXISTS url_provenance")
        cur.execute("SELECT 1 FROM information_schema.columns WHERE table_schema='sieve' "
                    "AND table_name='rules' AND column_name='superseded_by'")
        assert cur.fetchone(), 'precondition: superseded_by present'
        cur.execute("SELECT 1 FROM information_schema.columns WHERE table_schema='sieve' "
                    "AND table_name='rules' AND column_name='url_provenance'")
        assert cur.fetchone() is None, 'precondition: url_provenance absent'
    dbm.init_schema(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM information_schema.columns WHERE table_schema='sieve' "
                    "AND table_name='rules' AND column_name='url_provenance'")
        assert cur.fetchone(), 'init_schema added the missing newer column'
    # And a real insert through upsert_rule now works (would have thrown before).
    out = dbm.upsert_rule(conn, {'name': 'x', 'if_condition': 'a', 'then_logic': 'b'},
                          doc_id='1', source_url='https://e.t/x', source_org='T')
    assert out == 'new'


def test_new_rules_carry_extracted_provenance(conn, web, fake_llm):
    add_source(conn)
    web.sitemap['https://example.test/sitemap.xml'] = [
        ('https://example.test/guide', None)]
    web.pages['https://example.test/guide'] = SEO_PAGE.format('x')
    _cycle()
    assert q1(conn, "SELECT url_provenance, provenance_status, "
                    "source_excerpt IS NOT NULL, source_content_hash IS NOT NULL "
                    "FROM sieve.rules") == ('extracted', 'verified_excerpt', True, True)


def test_quality_gate_rejects_paraphrased_source_excerpt():
    from sieve_ingest import extract
    source = 'Google recommends using descriptive page titles in search results.'
    rules = [{'name': 'Write titles', 'if_condition': 'a page exists',
              'then_logic': 'use a descriptive title',
              'source_excerpt': 'Always add perfect title tags for higher rankings.',
              'confidence_score': 0.99}]
    kept, rejected = extract._validate_rules(rules, 'https://example.test', source)
    assert kept == [] and rejected == 1


def test_quality_gate_requires_case_faithful_source_excerpt():
    from sieve_ingest import extract
    source = 'Google recommends using descriptive page titles in search results.'
    rules = [{'name': 'Write titles', 'if_condition': 'a page exists',
              'then_logic': 'use a descriptive title',
              'source_excerpt': 'google recommends using descriptive page titles',
              'confidence_score': 0.99}]
    kept, rejected = extract._validate_rules(rules, 'https://example.test', source)
    assert kept == [] and rejected == 1


def test_long_docs_chunk_instead_of_truncate():
    from sieve_ingest.extract import _chunks, CHUNK_CHARS
    short = 'a' * 100
    assert _chunks(short) == [short]
    lines = ('para ' * 400 + '\n') * 12  # ~24k chars with newlines
    long_text = lines
    chunks = _chunks(long_text)
    assert len(chunks) == 2
    assert all(len(c) <= CHUNK_CHARS for c in chunks)
    # the split point content is preserved across the boundary
    assert chunks[0] + chunks[1] in long_text or (chunks[0] in long_text and chunks[1] in long_text)


def test_gone_without_prior_state_is_not_removed(conn, web, fake_llm):
    """A 404 on a page we never fingerprinted is noise, not a retire signal."""
    add_source(conn)
    web.sitemap['https://example.test/sitemap.xml'] = [
        ('https://example.test/ghost', None)]
    # no page registered → handler 404s
    summary = _cycle()
    assert summary['rules_retired'] == 0
    assert q1(conn, "SELECT count(*) FROM sieve.ingest_changes")[0] == 0
