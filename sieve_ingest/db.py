"""
db.py — central-brain access for the ingestion agent.

Writes to the SAME Railway Postgres the auditor reads (schema `sieve`). Owns its
own control tables (source_registry, ingest_runs, ingest_changes) and the write
path for brain objects with provenance + versioning.

Env:
    SIEVE_DB_URL  (or DATABASE_URL) — the central Postgres

Never silently loses provenance: every ingested rule/principle stamps
source_org, source_url, document_id, extracted_at, last_verified.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any, Dict, List, Optional

log = logging.getLogger('ingest.db')

DB_URL = os.getenv('SIEVE_DB_URL') or os.getenv('DATABASE_URL')

# Control tables live in the `sieve` schema alongside the brain.
CONTROL_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS sieve;

-- The governed list of sources: what to crawl, how authoritative, how often.
CREATE TABLE IF NOT EXISTS sieve.source_registry (
    source_id      text PRIMARY KEY,        -- stable slug, e.g. 'google-search-central'
    canonical_org  text NOT NULL,           -- ranker-canonical org name, e.g. 'Google'
    adapter_type   text NOT NULL DEFAULT 'sitemap',  -- sitemap | github_release | changelog | rss
    tier           smallint NOT NULL DEFAULT 3,
    root_url       text NOT NULL,
    sitemap_url    text,
    seed_urls      jsonb,                        -- explicit exact-page URLs for the url_list adapter
    url_filter     text,                         -- allow-regex on URL path; sitemap URLs not matching are skipped
    crawl_cadence_days int NOT NULL DEFAULT 30,
    last_crawled_at timestamptz,
    last_seen_marker text,                   -- version tag / max lastmod / feed ts
    enabled        boolean NOT NULL DEFAULT true,
    notes          text,
    consecutive_failures int NOT NULL DEFAULT 0,  -- health: reset on any ok cycle
    last_ok_at     timestamptz,                   -- health: last cycle with status=ok
    last_error     text                           -- health: most recent failure detail
);

-- One row per ingestion cycle (for observability + 'when did we last refresh').
CREATE TABLE IF NOT EXISTS sieve.ingest_runs (
    run_id       bigserial PRIMARY KEY,
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    sources_checked int DEFAULT 0,
    sources_changed int DEFAULT 0,
    urls_changed int DEFAULT 0,
    objects_written int DEFAULT 0,
    status       text DEFAULT 'running',
    detail       jsonb
);

-- One row per detected change (the freshness audit trail — 'what changed').
CREATE TABLE IF NOT EXISTS sieve.ingest_changes (
    change_id    bigserial PRIMARY KEY,
    run_id       bigint,
    source_id    text,
    url          text,
    change_type  text,                       -- new | modified | unchanged | removed
    signal       text,                       -- lastmod | etag | content_hash | version
    old_hash     text,
    new_hash     text,
    detected_at  timestamptz NOT NULL DEFAULT now(),
    extract_status text NOT NULL DEFAULT 'detected',  -- detected | extracted | empty | irrelevant | failed | gave_up
    rules_new    int,
    rules_refreshed int
);

-- Per-URL fingerprint so we can tell what actually changed next cycle.
CREATE TABLE IF NOT EXISTS sieve.url_state (
    url          text PRIMARY KEY,
    source_id    text,
    etag         text,
    last_modified text,
    content_hash text,
    last_seen_at timestamptz NOT NULL DEFAULT now()
);

-- count_extract_failures() runs per failed change; keep it index-backed as
-- ingest_changes grows.
CREATE INDEX IF NOT EXISTS ingest_changes_url_hash_idx
    ON sieve.ingest_changes (url, new_hash);
"""


def connect():
    if not DB_URL:
        raise RuntimeError('SIEVE_DB_URL / DATABASE_URL not set')
    import psycopg2
    conn = psycopg2.connect(DB_URL, connect_timeout=20)
    conn.autocommit = True
    return conn


def init_schema(conn=None) -> None:
    own = conn is None
    conn = conn or connect()
    try:
        with conn.cursor() as cur:
            cur.execute(CONTROL_SCHEMA)
            # Columns may be absent on tables created before they were added.
            cur.execute("ALTER TABLE sieve.source_registry "
                        "ADD COLUMN IF NOT EXISTS seed_urls jsonb, "
                        "ADD COLUMN IF NOT EXISTS url_filter text, "
                        "ADD COLUMN IF NOT EXISTS consecutive_failures int NOT NULL DEFAULT 0, "
                        "ADD COLUMN IF NOT EXISTS last_ok_at timestamptz, "
                        "ADD COLUMN IF NOT EXISTS last_error text, "
                        "ADD COLUMN IF NOT EXISTS crawl_cursor jsonb")
            # Sequence-based ids for brain inserts: MAX(id)+1 races when the CLI
            # and the cron run concurrently. Guarded setval never rewinds.
            for t in ('rules', 'documents'):
                cur.execute(f"CREATE SEQUENCE IF NOT EXISTS sieve.{t}_ingest_id_seq")
                cur.execute(f"""
                    SELECT setval('sieve.{t}_ingest_id_seq', GREATEST(
                        (SELECT last_value FROM sieve.{t}_ingest_id_seq),
                        (SELECT COALESCE(MAX(NULLIF(id,'')::bigint),0) FROM sieve.{t}
                         WHERE id ~ '^[0-9]+$')))
                """)
            cur.execute("ALTER TABLE sieve.ingest_changes "
                        "ADD COLUMN IF NOT EXISTS extract_status text NOT NULL DEFAULT 'detected', "
                        "ADD COLUMN IF NOT EXISTS rules_new int, "
                        "ADD COLUMN IF NOT EXISTS rules_refreshed int")
            # Provenance columns on the brain tables for newly-ingested rows.
            # Even a no-op ALTER takes an ACCESS EXCLUSIVE lock, and the live
            # auditor reads these tables — only ALTER when actually missing.
            for t in ('rules', 'principles', 'anti_patterns'):
                cur.execute("SELECT 1 FROM information_schema.columns "
                            "WHERE table_schema='sieve' AND table_name=%s "
                            "AND column_name='superseded_by'", (t,))
                if cur.fetchone():
                    continue
                cur.execute(f"""
                    ALTER TABLE sieve.{t}
                        ADD COLUMN IF NOT EXISTS source_url text,
                        ADD COLUMN IF NOT EXISTS document_id text,
                        ADD COLUMN IF NOT EXISTS extracted_at timestamptz,
                        ADD COLUMN IF NOT EXISTS last_verified timestamptz,
                        ADD COLUMN IF NOT EXISTS rule_key text,
                        ADD COLUMN IF NOT EXISTS superseded_by text
                """)
        log.info('control schema + provenance columns ready')
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def upsert_source(conn, s: Dict[str, Any], force: bool = False) -> None:
    """Insert a source row. By default EXISTING rows are left untouched so
    operator fixes in the DB survive the every-cycle re-seed (this clobbering
    already caused live registry drift once). `force=True` is the deliberate
    code-driven sync path (`seed --force`) — it updates everything EXCEPT
    `enabled`, so an operator disable always wins."""
    from psycopg2.extras import Json
    params = {'adapter_type': 'sitemap', 'tier': 3, 'sitemap_url': None,
              'seed_urls': None, 'url_filter': None, 'crawl_cadence_days': 30,
              'enabled': True, 'notes': None, **s}
    params['seed_urls'] = Json(params['seed_urls']) if params.get('seed_urls') else None
    conflict = """
            ON CONFLICT (source_id) DO UPDATE SET
                canonical_org=EXCLUDED.canonical_org, adapter_type=EXCLUDED.adapter_type,
                tier=EXCLUDED.tier, root_url=EXCLUDED.root_url, sitemap_url=EXCLUDED.sitemap_url,
                seed_urls=EXCLUDED.seed_urls, url_filter=EXCLUDED.url_filter,
                crawl_cadence_days=EXCLUDED.crawl_cadence_days, notes=EXCLUDED.notes
    """ if force else " ON CONFLICT (source_id) DO NOTHING"
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sieve.source_registry
                (source_id, canonical_org, adapter_type, tier, root_url,
                 sitemap_url, seed_urls, url_filter, crawl_cadence_days, enabled, notes)
            VALUES (%(source_id)s,%(canonical_org)s,%(adapter_type)s,%(tier)s,%(root_url)s,
                    %(sitemap_url)s,%(seed_urls)s,%(url_filter)s,%(crawl_cadence_days)s,
                    %(enabled)s,%(notes)s)
        """ + conflict, params)


def due_sources(conn) -> List[Dict[str, Any]]:
    """Sources whose cadence has elapsed (or never crawled). The 12h grace keeps
    a weekly source due at the weekly cron slot: crawled Monday 06:03 must be due
    again NEXT Monday 06:00, not slip to the week after."""
    from psycopg2.extras import RealDictCursor
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT * FROM sieve.source_registry
            WHERE enabled
              AND (last_crawled_at IS NULL
                   OR last_crawled_at < now() - (crawl_cadence_days || ' days')::interval
                                              + interval '12 hours')
            ORDER BY tier ASC, last_crawled_at ASC NULLS FIRST
        """)
        return [dict(r) for r in cur.fetchall()]


def mark_source_crawled(conn, source_id: str, marker: Optional[str]) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE sieve.source_registry SET last_crawled_at=now(), "
                    "last_seen_marker=COALESCE(%s, last_seen_marker) WHERE source_id=%s",
                    (marker, source_id))


def get_crawl_cursor(conn, source_id: str) -> Dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT crawl_cursor FROM sieve.source_registry WHERE source_id=%s",
                    (source_id,))
        row = cur.fetchone()
        return (row[0] or {}) if row else {}


def save_crawl_cursor(conn, source_id: str, cursor: Dict[str, Any]) -> None:
    from psycopg2.extras import Json
    with conn.cursor() as cur:
        cur.execute("UPDATE sieve.source_registry SET crawl_cursor=%s WHERE source_id=%s",
                    (Json(cursor), source_id))


def pending_retry_urls(conn, source_id: str, limit: int = 20) -> List[str]:
    """URLs whose most recent change is still 'failed' (extraction never
    succeeded, change never consumed). The rotation cursor must re-probe these
    FIRST or they'd be stranded until the window wraps back around."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (url) url, extract_status
            FROM sieve.ingest_changes
            WHERE source_id=%s
            ORDER BY url, change_id DESC
        """, (source_id,))
        return [u for u, st in cur.fetchall() if st == 'failed'][:limit]


def update_source_health(conn, source_id: str, ok: bool, error: Optional[str] = None) -> int:
    """Health ledger per source: ok resets the failure streak; a failure
    increments it. Returns the new consecutive_failures count (alerting keys
    off this in the Phase-2 digest)."""
    with conn.cursor() as cur:
        if ok:
            cur.execute("UPDATE sieve.source_registry SET consecutive_failures=0, "
                        "last_ok_at=now(), last_error=NULL WHERE source_id=%s "
                        "RETURNING consecutive_failures", (source_id,))
        else:
            cur.execute("UPDATE sieve.source_registry SET "
                        "consecutive_failures=consecutive_failures+1, last_error=%s "
                        "WHERE source_id=%s RETURNING consecutive_failures",
                        ((error or '')[:300], source_id))
        row = cur.fetchone()
        return row[0] if row else 0


# ---------------------------------------------------------------------------
# URL fingerprints + change log
# ---------------------------------------------------------------------------

def get_url_state(conn, url: str) -> Optional[Dict[str, Any]]:
    from psycopg2.extras import RealDictCursor
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM sieve.url_state WHERE url=%s", (url,))
        r = cur.fetchone()
        return dict(r) if r else None


def save_url_state(conn, url: str, source_id: str, etag, last_modified, content_hash) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sieve.url_state (url, source_id, etag, last_modified, content_hash, last_seen_at)
            VALUES (%s,%s,%s,%s,%s, now())
            ON CONFLICT (url) DO UPDATE SET
                etag=EXCLUDED.etag, last_modified=EXCLUDED.last_modified,
                content_hash=EXCLUDED.content_hash, last_seen_at=now()
        """, (url, source_id, etag, last_modified, content_hash))


def record_change(conn, run_id, source_id, url, change_type, signal, old_hash, new_hash) -> int:
    """Record a detected change (extract_status='detected'); returns change_id so
    the extraction outcome can be written back onto the same row."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sieve.ingest_changes
                (run_id, source_id, url, change_type, signal, old_hash, new_hash)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            RETURNING change_id
        """, (run_id, source_id, url, change_type, signal, old_hash, new_hash))
        return cur.fetchone()[0]


def update_change_outcome(conn, change_id: int, extract_status: str,
                          rules_new: int = 0, rules_refreshed: int = 0) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE sieve.ingest_changes SET extract_status=%s, rules_new=%s, "
                    "rules_refreshed=%s WHERE change_id=%s",
                    (extract_status, rules_new, rules_refreshed, change_id))


def count_extract_failures(conn, url: str, new_hash: str) -> int:
    """How many times extraction has already failed for this exact url+content
    version. Backstop for the retry loop: after GAVE_UP_AFTER failures the change
    is consumed with extract_status='gave_up' instead of retrying forever.
    Stale 'detected' rows (>1h old) count too — those are attempts that crashed
    before the outcome write and must not escape the cap."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) FROM sieve.ingest_changes
            WHERE url=%s AND new_hash=%s
              AND (extract_status IN ('failed','gave_up')
                   OR (extract_status='detected'
                       AND detected_at < now() - interval '1 hour'))
        """, (url, new_hash))
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

def start_run(conn) -> int:
    with conn.cursor() as cur:
        # Sweep orphans first: a crashed run must not sit 'running' forever.
        cur.execute("UPDATE sieve.ingest_runs SET status='aborted', finished_at=now() "
                    "WHERE status='running' AND started_at < now() - interval '2 hours'")
        if cur.rowcount:
            log.warning('swept %d stale running run(s) → aborted', cur.rowcount)
        cur.execute("INSERT INTO sieve.ingest_runs DEFAULT VALUES RETURNING run_id")
        return cur.fetchone()[0]


def finish_run(conn, run_id, status: str = 'done', detail: Optional[Dict] = None,
               **fields) -> None:
    """status: done (all sources clean) | partial (some source/url failed) |
    failed (the run itself crashed). detail carries the per-source breakdown."""
    from psycopg2.extras import Json
    sets = ', '.join(f"{k}=%s" for k in fields)
    sets = (sets + ', ' if sets else '') + 'status=%s, detail=%s'
    with conn.cursor() as cur:
        cur.execute(f"UPDATE sieve.ingest_runs SET finished_at=now(), {sets} "
                    f"WHERE run_id=%s",
                    (*fields.values(), status, Json(detail) if detail else None, run_id))


# ---------------------------------------------------------------------------
# Brain writes (documents + rules) with provenance + dedupe/version
# ---------------------------------------------------------------------------

def upsert_document(conn, source_url, source_org, title, domain_tag) -> str:
    """Insert/refresh a source document row; returns its id (text)."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM sieve.documents WHERE source_url=%s LIMIT 1", (source_url,))
        row = cur.fetchone()
        if row:
            cur.execute("UPDATE sieve.documents SET title=%s WHERE id=%s", (title, row[0]))
            return row[0]
        cur.execute("SELECT nextval('sieve.documents_ingest_id_seq')")
        new_id = str(cur.fetchone()[0])
        cur.execute("""
            INSERT INTO sieve.documents (id, title, source_type, domain_tag, source_url,
                                         source_org, created_at)
            VALUES (%s,%s,'ingest',%s,%s,%s, now())
        """, (new_id, title, domain_tag, source_url, source_org))
        return new_id


def _rule_key(name: str, if_cond: str) -> str:
    return 'rk_' + hashlib.sha256(f'{(name or "").strip().lower()}|{(if_cond or "").strip().lower()}'
                                  .encode()).hexdigest()[:20]


def upsert_rule(conn, rule: Dict[str, Any], doc_id: str, source_url: str,
                source_org: str) -> str:
    """Insert a rule, or if a rule with the same rule_key exists, refresh its
    last_verified (proof it's still current) instead of duplicating. Returns
    'new' | 'refreshed'."""
    key = _rule_key(rule.get('name', ''), rule.get('if_condition', ''))
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM sieve.rules WHERE rule_key=%s LIMIT 1", (key,))
        existing = cur.fetchone()
        if existing:
            # Refresh last_verified; and BACKFILL/UPGRADE source_url when the new
            # page is more specific (more path depth) or the existing has none.
            # Never blanks an existing URL — only improves it. No data lost.
            cur.execute("SELECT source_url FROM sieve.rules WHERE rule_key=%s", (key,))
            cur_url = (cur.fetchone()[0] or '')
            more_specific = bool(source_url) and (
                not cur_url or
                source_url.rstrip('/').count('/') > cur_url.rstrip('/').count('/'))
            if more_specific:
                cur.execute("UPDATE sieve.rules SET source_url=%s, document_id=%s, "
                            "last_verified=now() WHERE rule_key=%s",
                            (source_url, doc_id, key))
            else:
                cur.execute("UPDATE sieve.rules SET last_verified=now() WHERE rule_key=%s", (key,))
            return 'refreshed'
        cur.execute("SELECT nextval('sieve.rules_ingest_id_seq')")
        new_id = str(cur.fetchone()[0])
        cur.execute("""
            INSERT INTO sieve.rules
                (id, name, rule_type, if_condition, then_logic, domain_tag,
                 confidence_score, source_refs_json, status, created_at,
                 source_org, source_url, document_id, extracted_at, last_verified, rule_key)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'active', now(),
                    %s,%s,%s, now(), now(), %s)
        """, (new_id, rule.get('name'), rule.get('rule_type', 'ingested'),
              rule.get('if_condition'), rule.get('then_logic'), rule.get('domain_tag', 'general'),
              str(rule.get('confidence_score', 0.8)), f'[{doc_id}]',
              source_org, source_url, doc_id, key))
        return 'new'
