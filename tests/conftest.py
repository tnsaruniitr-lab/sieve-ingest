"""Test harness: scratch Postgres (started by tests/run-local.sh) + per-test
truncation. SIEVE_DB_URL must point at a DISPOSABLE database — conftest creates
the sieve schema and minimal brain tables there."""

import os

import pytest

DSN = os.environ.get('SIEVE_DB_URL', '')
if not DSN:
    pytest.skip('SIEVE_DB_URL not set — start the scratch PG with tests/run-local.sh',
                allow_module_level=True)
if 'sieve_test' not in DSN or 'railway' in DSN or 'rlwy.net' in DSN:
    raise RuntimeError('tests TRUNCATE sieve.rules — SIEVE_DB_URL must point at a '
                       'database literally named sieve_test, never prod')

# Minimal brain tables matching the prod columns the ingest write path touches.
# init_schema() ALTERs these with provenance columns — they must exist first.
BRAIN_TABLES = """
CREATE SCHEMA IF NOT EXISTS sieve;
CREATE TABLE IF NOT EXISTS sieve.rules (
    id text PRIMARY KEY, name text, rule_type text, if_condition text,
    then_logic text, domain_tag text, confidence_score text,
    source_refs_json text, status text, created_at timestamptz, source_org text);
CREATE TABLE IF NOT EXISTS sieve.documents (
    id text PRIMARY KEY, title text, source_type text, domain_tag text,
    source_url text, source_org text, created_at timestamptz);
CREATE TABLE IF NOT EXISTS sieve.principles (id text PRIMARY KEY, name text);
CREATE TABLE IF NOT EXISTS sieve.anti_patterns (id text PRIMARY KEY, name text);
"""


@pytest.fixture(scope='session')
def _bootstrap():
    import psycopg2
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(BRAIN_TABLES)
    conn.close()


@pytest.fixture
def conn(_bootstrap):
    from sieve_ingest import db
    c = db.connect()
    db.init_schema(c)
    with c.cursor() as cur:
        cur.execute("""
            TRUNCATE sieve.source_registry, sieve.ingest_runs, sieve.ingest_changes,
                     sieve.url_state, sieve.rules, sieve.documents RESTART IDENTITY
        """)
    yield c
    c.close()


class MockWeb:
    """In-memory site served through httpx.MockTransport. Tests mutate .pages /
    .fail_urls between cycles to simulate content changes and outages."""

    def __init__(self):
        self.pages = {}       # url -> html body
        self.sitemap = {}     # sitemap url -> [(loc, lastmod), ...]
        self.fail_urls = set()
        self.requests = []

    def handler(self, request):
        import httpx
        url = str(request.url)
        self.requests.append(url)
        if url in self.fail_urls:
            raise httpx.ConnectError('mock outage', request=request)
        if url in self.sitemap:
            urls = ''.join(
                f'<url><loc>{loc}</loc>' + (f'<lastmod>{lm}</lastmod>' if lm else '')
                + '</url>' for loc, lm in self.sitemap[url])
            body = ('<?xml version="1.0"?><urlset '
                    'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    f'{urls}</urlset>')
            return httpx.Response(200, text=body)
        if url in self.pages:
            return httpx.Response(200, text=self.pages[url],
                                  headers={'ETag': f'W/"{hash(self.pages[url])}"'})
        return httpx.Response(404, text='not found')


from sieve_ingest import registry as _registry

ORIG_SEED = list(_registry.SEED_SOURCES)


@pytest.fixture(autouse=True)
def _no_default_seeds(monkeypatch):
    """run_cycle() re-seeds every cycle; tests control their own registry rows.
    The seed-behavior test restores ORIG_SEED explicitly."""
    monkeypatch.setattr(_registry, 'SEED_SOURCES', [])


@pytest.fixture
def web(monkeypatch):
    import httpx
    from sieve_ingest import freshness
    site = MockWeb()
    real_client = httpx.Client  # capture BEFORE patching — the factory must not recurse

    def client_factory(**kwargs):
        return real_client(transport=httpx.MockTransport(site.handler),
                           follow_redirects=True)

    monkeypatch.setattr(freshness.httpx, 'Client', client_factory)
    return site


@pytest.fixture
def fake_llm(monkeypatch):
    """Replace the Anthropic call. state['mode']: ok | fail | empty.
    state['calls'] counts invocations (proves the relevance screen saved spend)."""
    from sieve_ingest import extract
    state = {'mode': 'ok', 'calls': 0}

    def _fake(text, org, url):
        state['calls'] += 1
        if state['mode'] == 'fail':
            raise extract.ExtractError('simulated LLM outage')
        if state['mode'] == 'empty':
            return []
        excerpt = ' '.join(str(text).split())[:160]
        return [{'name': f'Rule from {url.rsplit("/", 1)[-1]}',
                 'if_condition': f'situation on {url}',
                 'then_logic': 'do the right thing',
                 'source_excerpt': excerpt,
                 'domain_tag': 'seo', 'confidence_score': 0.9}]

    monkeypatch.setattr(extract, '_extract_rules', _fake)
    return state


def add_source(conn, **over):
    from sieve_ingest import db
    s = dict(source_id='test-src', canonical_org='TestOrg', tier=1,
             adapter_type='sitemap', crawl_cadence_days=7,
             root_url='https://example.test',
             sitemap_url='https://example.test/sitemap.xml')
    s.update(over)
    db.upsert_source(conn, s, force=True)
    return s


def make_due(conn, source_id='test-src'):
    with conn.cursor() as cur:
        cur.execute("UPDATE sieve.source_registry SET last_crawled_at=NULL "
                    "WHERE source_id=%s", (source_id,))


def q1(conn, sql, *args):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchone()
