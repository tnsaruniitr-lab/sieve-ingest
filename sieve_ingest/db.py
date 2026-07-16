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
import json
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
    crawl_cadence_days int NOT NULL DEFAULT 30,
    last_crawled_at timestamptz,
    last_seen_marker text,                   -- version tag / max lastmod / feed ts
    enabled        boolean NOT NULL DEFAULT true,
    notes          text
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
    detected_at  timestamptz NOT NULL DEFAULT now()
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
            # seed_urls may be absent on a source_registry created before this column.
            cur.execute("ALTER TABLE sieve.source_registry "
                        "ADD COLUMN IF NOT EXISTS seed_urls jsonb")
            # Provenance columns on the brain tables for newly-ingested rows.
            for t in ('rules', 'principles', 'anti_patterns', 'playbooks'):
                cur.execute(f"""
                    ALTER TABLE sieve.{t}
                        ADD COLUMN IF NOT EXISTS source_url text,
                        ADD COLUMN IF NOT EXISTS document_id text,
                        ADD COLUMN IF NOT EXISTS extracted_at timestamptz,
                        ADD COLUMN IF NOT EXISTS last_verified timestamptz,
                        ADD COLUMN IF NOT EXISTS rule_key text,
                        ADD COLUMN IF NOT EXISTS superseded_by text,
                        ADD COLUMN IF NOT EXISTS url_provenance text
                """)
        log.info('control schema + provenance columns ready')
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def upsert_source(conn, s: Dict[str, Any]) -> None:
    from psycopg2.extras import Json
    params = {'adapter_type': 'sitemap', 'tier': 3, 'sitemap_url': None,
              'seed_urls': None, 'crawl_cadence_days': 30, 'enabled': True,
              'notes': None, **s}
    params['seed_urls'] = Json(params['seed_urls']) if params.get('seed_urls') else None
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sieve.source_registry
                (source_id, canonical_org, adapter_type, tier, root_url,
                 sitemap_url, seed_urls, crawl_cadence_days, enabled, notes)
            VALUES (%(source_id)s,%(canonical_org)s,%(adapter_type)s,%(tier)s,%(root_url)s,
                    %(sitemap_url)s,%(seed_urls)s,%(crawl_cadence_days)s,%(enabled)s,%(notes)s)
            ON CONFLICT (source_id) DO UPDATE SET
                canonical_org=EXCLUDED.canonical_org, adapter_type=EXCLUDED.adapter_type,
                tier=EXCLUDED.tier, root_url=EXCLUDED.root_url, sitemap_url=EXCLUDED.sitemap_url,
                seed_urls=EXCLUDED.seed_urls, enabled=EXCLUDED.enabled,
                crawl_cadence_days=EXCLUDED.crawl_cadence_days, notes=EXCLUDED.notes
        """, params)


def due_sources(conn) -> List[Dict[str, Any]]:
    """Sources whose cadence has elapsed (or never crawled)."""
    from psycopg2.extras import RealDictCursor
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT * FROM sieve.source_registry
            WHERE enabled
              AND (last_crawled_at IS NULL
                   OR last_crawled_at < now() - (crawl_cadence_days || ' days')::interval)
            ORDER BY tier ASC, last_crawled_at ASC NULLS FIRST
        """)
        return [dict(r) for r in cur.fetchall()]


def mark_source_crawled(conn, source_id: str, marker: Optional[str]) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE sieve.source_registry SET last_crawled_at=now(), "
                    "last_seen_marker=COALESCE(%s, last_seen_marker) WHERE source_id=%s",
                    (marker, source_id))


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


def record_change(conn, run_id, source_id, url, change_type, signal, old_hash, new_hash) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sieve.ingest_changes
                (run_id, source_id, url, change_type, signal, old_hash, new_hash)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, (run_id, source_id, url, change_type, signal, old_hash, new_hash))


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

def start_run(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO sieve.ingest_runs DEFAULT VALUES RETURNING run_id")
        return cur.fetchone()[0]


def finish_run(conn, run_id, **fields) -> None:
    fields.setdefault('status', 'done')
    sets = ', '.join(f"{k}=%s" for k in fields)
    with conn.cursor() as cur:
        cur.execute(f"UPDATE sieve.ingest_runs SET finished_at=now(), {sets} "
                    f"WHERE run_id=%s", (*fields.values(), run_id))


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
        cur.execute("SELECT COALESCE(MAX(NULLIF(id,'')::bigint),0)+1 FROM sieve.documents "
                    "WHERE id ~ '^[0-9]+$'")
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
                source_org: str, status: str = 'active',
                url_provenance: Optional[Dict[str, Any]] = None) -> str:
    """Insert a rule, or if a rule with the same rule_key exists, refresh its
    last_verified (proof it's still current) instead of duplicating. Returns
    'new' | 'refreshed'. status='candidate' + a custom url_provenance dict is
    the observed-rule path (crawl-derived knowledge, never authority-tier)."""
    key = _rule_key(rule.get('name', ''), rule.get('if_condition', ''))
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM sieve.rules WHERE rule_key=%s LIMIT 1", (key,))
        existing = cur.fetchone()
        if existing:
            # Refresh last_verified; BACKFILL/UPGRADE source_url when the new
            # page is more specific (more path depth) or the existing has none;
            # and BACKFILL then_logic if the existing row somehow has none.
            # Never blanks an existing URL or action — only improves. No data lost.
            cur.execute("SELECT source_url, then_logic FROM sieve.rules WHERE rule_key=%s", (key,))
            cur_url, cur_then = cur.fetchone()
            cur_url = cur_url or ''
            more_specific = bool(source_url) and (
                not cur_url or
                source_url.rstrip('/').count('/') > cur_url.rstrip('/').count('/'))
            if more_specific:
                cur.execute("""UPDATE sieve.rules SET source_url=%s, document_id=%s,
                            last_verified=now(),
                            url_provenance=%s
                            WHERE rule_key=%s""",
                            (source_url, doc_id,
                             json.dumps({'method': 'exact-upgrade', 'at': _now_iso()}), key))
            else:
                cur.execute("UPDATE sieve.rules SET last_verified=now() WHERE rule_key=%s", (key,))
            if (not cur_then or not str(cur_then).strip()) and rule.get('then_logic'):
                cur.execute("UPDATE sieve.rules SET then_logic=%s WHERE rule_key=%s",
                            (rule.get('then_logic'), key))
            return 'refreshed'
        cur.execute("SELECT COALESCE(MAX(NULLIF(id,'')::bigint),0)+1 FROM sieve.rules "
                    "WHERE id ~ '^[0-9]+$'")
        new_id = str(cur.fetchone()[0])
        prov = url_provenance or {'method': 'exact', 'at': _now_iso()}
        cur.execute("""
            INSERT INTO sieve.rules
                (id, name, rule_type, if_condition, then_logic, domain_tag,
                 confidence_score, source_refs_json, status, created_at,
                 source_org, source_url, document_id, extracted_at, last_verified, rule_key,
                 url_provenance)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, now(),
                    %s,%s,%s, now(), now(), %s, %s)
        """, (new_id, rule.get('name'), rule.get('rule_type', 'ingested'),
              rule.get('if_condition'), rule.get('then_logic'), rule.get('domain_tag', 'general'),
              str(rule.get('confidence_score', 0.8)), f'[{doc_id}]', status,
              source_org, source_url, doc_id, key,
              json.dumps(prov)))
        return 'new'


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec='seconds')
