"""Phase-1 drills: source health ledger, sequence ids, operator CLI."""

import subprocess
import sys

from conftest import add_source, q1

SEO_PAGE = ('<html><body><main><p>Structured data and sitemaps help search '
            'engines crawl, index and rank pages.</p></main></body></html>')


def _cycle():
    from sieve_ingest import agent
    return agent.run_cycle()


def test_health_ledger_increments_and_resets(conn, web, fake_llm, monkeypatch):
    from sieve_ingest import freshness
    add_source(conn)

    real_detect = freshness.detect

    def boom(conn_, source):
        raise RuntimeError('network down')
    monkeypatch.setattr(freshness, 'detect', boom)
    _cycle()
    _cycle()
    assert q1(conn, "SELECT consecutive_failures, last_error FROM "
                    "sieve.source_registry WHERE source_id='test-src'") == \
        (2, 'detect: network down')

    monkeypatch.setattr(freshness, 'detect', real_detect)
    web.sitemap['https://example.test/sitemap.xml'] = [
        ('https://example.test/guide', None)]
    web.pages['https://example.test/guide'] = SEO_PAGE
    _cycle()
    cf, ok_at, err = q1(conn, "SELECT consecutive_failures, last_ok_at, last_error "
                              "FROM sieve.source_registry WHERE source_id='test-src'")
    assert cf == 0 and ok_at is not None and err is None


def test_extract_failures_count_in_health(conn, web, fake_llm):
    add_source(conn)
    web.sitemap['https://example.test/sitemap.xml'] = [
        ('https://example.test/guide', None)]
    web.pages['https://example.test/guide'] = SEO_PAGE
    fake_llm['mode'] = 'fail'
    _cycle()
    assert q1(conn, "SELECT consecutive_failures FROM sieve.source_registry "
                    "WHERE source_id='test-src'")[0] == 1


def test_sequence_ids_are_distinct_and_monotonic(conn):
    from sieve_ingest import db as dbm
    # Simulate a pre-existing corpus with a high numeric id, then insert twice.
    with conn.cursor() as cur:
        cur.execute("INSERT INTO sieve.rules (id, name, status) "
                    "VALUES ('9000', 'legacy', 'active')")
    dbm.init_schema(conn)  # setval picks up the max
    r1 = dbm.upsert_rule(conn, {'name': 'a', 'if_condition': 'x', 'then_logic': 'y'},
                         doc_id='1', source_url='https://e.t/a', source_org='T')
    r2 = dbm.upsert_rule(conn, {'name': 'b', 'if_condition': 'x2', 'then_logic': 'y'},
                         doc_id='1', source_url='https://e.t/b', source_org='T')
    assert (r1, r2) == ('new', 'new')
    ids = [int(i) for (i,) in _all(conn, "SELECT id FROM sieve.rules "
                                         "WHERE id ~ '^[0-9]+$' ORDER BY id::bigint")]
    assert ids[-2] > 9000 and ids[-1] == ids[-2] + 1, 'sequence continues past legacy max'


def _all(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def _cli(*args):
    return subprocess.run([sys.executable, '-m', 'sieve_ingest', *args],
                          capture_output=True, text=True, cwd='/tmp/sieve-ingest-work')


def test_set_source_and_toggle_survive_reseed(conn):
    import conftest
    from sieve_ingest import registry
    add_source(conn)
    r = _cli('set-source', 'test-src', 'sitemap_url=https://example.test/fixed.xml',
             'crawl_cadence_days=14')
    assert r.returncode == 0, r.stderr
    r = _cli('disable', 'test-src')
    assert r.returncode == 0, r.stderr
    registry.seed(conn)  # the every-cycle insert-only re-seed
    assert q1(conn, "SELECT sitemap_url, crawl_cadence_days, enabled FROM "
                    "sieve.source_registry WHERE source_id='test-src'") == \
        ('https://example.test/fixed.xml', 14, False)


def test_set_source_rejects_unknown_field(conn):
    add_source(conn)
    r = _cli('set-source', 'test-src', 'nonsense=1')
    assert r.returncode == 1 and 'unknown field' in r.stdout


def test_health_command_output(conn, web, fake_llm, monkeypatch):
    from sieve_ingest import freshness
    add_source(conn)
    monkeypatch.setattr(freshness, 'detect',
                        lambda c, s: (_ for _ in ()).throw(RuntimeError('boom')))
    _cycle(); _cycle(); _cycle()
    r = _cli('health')
    assert r.returncode == 0
    assert '!!' in r.stdout and 'fails=3' in r.stdout, r.stdout
