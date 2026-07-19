"""Loop-repair + freshness re-verification drills.

Half 1 — the chat-driven enrichment loop (harvest_pages.py / ingest_extracted.py,
the ONLY extraction path while the cron is monitor-only) must import cleanly
against the current extract.py API and dry-run end to end.

Half 2 — an unchanged observation (304 / same content hash) is evidence, not a
non-event: db.refresh_verified_for_url stamps last_verified=now() on the citable
brain rows citing that URL (rules + principles/anti_patterns/playbooks where the
columns exist), monitor mode included; rejected/retired/superseded/deprecated
rows are never touched."""

import importlib.util
import json
import sys
from pathlib import Path

from conftest import add_source, make_due, q1

REPO = Path(__file__).resolve().parents[1]

SEO_PAGE = ('<html><head><title>Structured data guide</title></head><body><main>'
            '<h1>Structured data guide</h1>'
            '<p>Use JSON-LD structured data so search engines can index and '
            'rank your pages. Add a sitemap and robots.txt so crawling works, '
            'keep canonical URLs stable, and write one meta description per '
            'page. Rich results depend on valid schema.org markup; test it '
            'before shipping and keep your title tag under sixty characters '
            'so snippets render fully in search.</p></main></body></html>')


def _cycle():
    from sieve_ingest import agent
    return agent.run_cycle()


def _load_script(name, argv, monkeypatch):
    """Import a repo-root loop script with a controlled argv (both parse
    sys.argv at import time — pytest's own argv must not leak in)."""
    monkeypatch.setattr(sys, 'argv', argv)
    spec = importlib.util.spec_from_file_location(name, REPO / f'{name}.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Half 1 — the file-bridge loop scripts work against the current extract API
# ---------------------------------------------------------------------------

def test_loop_scripts_import_cleanly(monkeypatch):
    """Regression: the b9e468d extract.py rewrite dropped _fetch_text and
    _valid_rule; both scripts must import + argument-parse on the current API."""
    hp = _load_script('harvest_pages', ['harvest_pages.py', 'out.jsonl', '7'],
                      monkeypatch)
    assert hp.OUT == 'out.jsonl' and hp.MAX_PER_SOURCE == 7
    assert callable(hp.main) and callable(hp._fetch_text)

    ie = _load_script('ingest_extracted', ['ingest_extracted.py', 'in.jsonl'],
                      monkeypatch)
    assert ie.IN == 'in.jsonl'
    assert callable(ie.main)
    # The extract API surface both scripts now depend on.
    from sieve_ingest import extract
    assert callable(extract._validate_rules) and callable(extract._rule_status)


def test_harvest_dry_run_writes_jsonl_without_consuming(conn, web, tmp_path,
                                                        monkeypatch):
    add_source(conn)
    web.sitemap['https://example.test/sitemap.xml'] = [
        ('https://example.test/guide', None)]
    web.pages['https://example.test/guide'] = SEO_PAGE

    out = tmp_path / 'harvest.jsonl'
    hp = _load_script('harvest_pages', ['harvest_pages.py', str(out), '5'],
                      monkeypatch)
    hp.main()

    lines = [json.loads(l) for l in out.read_text().splitlines()]
    assert len(lines) == 1
    page = lines[0]
    assert page['url'] == 'https://example.test/guide'
    assert page['source_id'] == 'test-src' and page['org'] == 'TestOrg'
    assert page['new_hash'] and len(page['text']) >= 200
    # Harvest must NOT consume: no url_state, no run row, source not crawled.
    assert q1(conn, "SELECT count(*) FROM sieve.url_state")[0] == 0
    assert q1(conn, "SELECT count(*) FROM sieve.ingest_runs")[0] == 0
    assert q1(conn, "SELECT last_crawled_at FROM sieve.source_registry")[0] is None


def test_ingest_extracted_commits_through_provenance_path(conn, tmp_path,
                                                          monkeypatch):
    add_source(conn)
    infile = tmp_path / 'extracted.jsonl'
    infile.write_text(json.dumps({
        'source_id': 'test-src', 'org': 'TestOrg',
        'url': 'https://example.test/guide', 'title': 'Guide',
        'new_hash': 'abc123', 'rules': [
            {'name': 'Use JSON-LD', 'if_condition': 'page has structured data',
             'then_logic': 'emit JSON-LD', 'domain_tag': 'seo',
             'confidence_score': 0.9},
            {'name': 'HowTo is dead', 'if_condition': 'page uses HowTo markup',
             'then_logic': 'HowTo rich results are no longer supported',
             'domain_tag': 'seo', 'confidence_score': 0.9,
             'status': 'deprecated'},
            {'name': 'Low conf', 'if_condition': 'x', 'then_logic': 'y',
             'domain_tag': 'seo', 'confidence_score': 0.2},  # below the floor
            'not-a-dict',
        ]}) + '\n')

    ie = _load_script('ingest_extracted', ['ingest_extracted.py', str(infile)],
                      monkeypatch)
    ie.main()

    # Two rules land (the 0.2-confidence one and the non-dict are rejected),
    # the LLM-marked deprecation takes the deprecated path like the cron.
    assert q1(conn, "SELECT count(*) FROM sieve.rules")[0] == 2
    assert q1(conn, "SELECT status, source_url, source_org FROM sieve.rules "
                    "WHERE name='Use JSON-LD'") == \
        ('active', 'https://example.test/guide', 'TestOrg')
    assert q1(conn, "SELECT status FROM sieve.rules "
                    "WHERE name='HowTo is dead'")[0] == 'deprecated'
    assert q1(conn, "SELECT count(*) FROM sieve.documents "
                    "WHERE source_url='https://example.test/guide'")[0] == 1
    # State advanced through the same provenance path as the cron.
    assert q1(conn, "SELECT content_hash FROM sieve.url_state "
                    "WHERE url='https://example.test/guide'")[0] == 'abc123'
    assert q1(conn, "SELECT extract_status, rules_new FROM sieve.ingest_changes")\
        == ('extracted', 2)
    assert q1(conn, "SELECT status, detail->>'_transport' FROM sieve.ingest_runs")\
        == ('done', 'local-claude-file-bridge')
    assert q1(conn, "SELECT last_crawled_at FROM sieve.source_registry")[0] \
        is not None


# ---------------------------------------------------------------------------
# Half 2 — freshness re-verification on unchanged signals
# ---------------------------------------------------------------------------

def test_unchanged_cycle_restamps_citing_rules(conn, web, fake_llm):
    add_source(conn)
    web.sitemap['https://example.test/sitemap.xml'] = [
        ('https://example.test/guide', None)]
    web.pages['https://example.test/guide'] = SEO_PAGE

    assert _cycle()['rules_written'] == 1
    with conn.cursor() as cur:
        cur.execute("UPDATE sieve.rules SET "
                    "last_verified = now() - interval '30 days'")

    # Cycle 2, content unchanged → same-hash observation re-stamps the rule.
    make_due(conn)
    summary = _cycle()
    assert summary['rules_verified'] == 1
    assert summary['urls_changed'] == 0
    assert q1(conn, "SELECT last_verified > now() - interval '1 minute' "
                    "FROM sieve.rules")[0] is True
    # The counts land in the run detail (ingest_runs notes) too.
    assert q1(conn, "SELECT detail->'sources'->0->>'verified_refreshed', "
                    "detail->'sources'->0->>'urls_unchanged' "
                    "FROM sieve.ingest_runs ORDER BY run_id DESC LIMIT 1") \
        == ('1', '1')


def test_monitor_mode_still_reverifies(conn, web, fake_llm, monkeypatch):
    """MAX_URLS_PER_SOURCE=0 skips extraction but the unchanged signal is
    still observed — re-verification must run exactly there."""
    from sieve_ingest import agent
    add_source(conn)
    web.sitemap['https://example.test/sitemap.xml'] = [
        ('https://example.test/guide', None)]
    web.pages['https://example.test/guide'] = SEO_PAGE

    assert _cycle()['rules_written'] == 1  # extraction on: rule + url_state land
    with conn.cursor() as cur:
        cur.execute("UPDATE sieve.rules SET "
                    "last_verified = now() - interval '30 days'")

    monkeypatch.setattr(agent, 'MAX_URLS_PER_SOURCE', 0)
    make_due(conn)
    summary = _cycle()
    assert summary['rules_verified'] == 1
    assert fake_llm['calls'] == 1, 'no LLM spend in monitor mode'
    assert q1(conn, "SELECT last_verified > now() - interval '1 minute' "
                    "FROM sieve.rules")[0] is True


def test_uncitable_rows_not_refreshed(conn):
    """rejected/retired/superseded/deprecated rows must never look freshly
    verified; active/candidate/NULL-status rows are citable and stamped."""
    from sieve_ingest import db
    url = 'https://example.test/x'
    statuses = ['active', 'candidate', None,
                'rejected', 'retired', 'superseded', 'deprecated']
    with conn.cursor() as cur:
        for i, st in enumerate(statuses):
            cur.execute("INSERT INTO sieve.rules (id, name, status, source_url, "
                        "last_verified) VALUES (%s,%s,%s,%s, "
                        "now() - interval '30 days')",
                        (f'r{i}', f'rule {i}', st, url))
        cur.execute("INSERT INTO sieve.rules (id, name, status, source_url, "
                    "last_verified) VALUES ('r-other','other url','active',"
                    "'https://example.test/other', now() - interval '30 days')")

    assert db.refresh_verified_for_url(conn, url) == 3
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM sieve.rules "
                    "WHERE last_verified > now() - interval '1 minute' "
                    "ORDER BY id")
        assert [r[0] for r in cur.fetchall()] == ['r0', 'r1', 'r2']


def test_non_rule_kinds_refreshed_when_source_url_matches(conn):
    """principles/anti_patterns (no status column in the fixture) and playbooks
    (with one) carry last_verified too — this is their first writer."""
    from sieve_ingest import db
    url = 'https://example.test/deep-dive'
    with conn.cursor() as cur:
        cur.execute("INSERT INTO sieve.principles (id, name, source_url) "
                    "VALUES ('p1','principle', %s)", (url,))
        cur.execute("INSERT INTO sieve.anti_patterns (id, name, source_url) "
                    "VALUES ('a1','anti-pattern', %s)", (url,))
        cur.execute("CREATE TABLE sieve.playbooks (id text PRIMARY KEY, "
                    "name text, status text, source_url text, "
                    "last_verified timestamptz)")
        cur.execute("INSERT INTO sieve.playbooks VALUES "
                    "('pb1','live playbook','active',%s,NULL), "
                    "('pb2','dead playbook','retired',%s,NULL)", (url, url))
    db._verify_targets_cache.clear()  # playbooks appeared mid-session
    try:
        assert db.refresh_verified_for_url(conn, url) == 3
        for table, ident in (('principles', 'p1'), ('anti_patterns', 'a1'),
                             ('playbooks', 'pb1')):
            assert q1(conn, f"SELECT last_verified > now() - interval '1 minute' "
                            f"FROM sieve.{table} WHERE id=%s", ident)[0] is True, table
        assert q1(conn, "SELECT last_verified FROM sieve.playbooks "
                        "WHERE id='pb2'")[0] is None, 'retired playbook untouched'
        assert db.refresh_verified_for_url(conn, 'https://example.test/none') == 0
    finally:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE sieve.playbooks")
        db._verify_targets_cache.clear()
