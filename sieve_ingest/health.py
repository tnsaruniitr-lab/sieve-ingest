"""
health.py — brain coverage metrics: views + weekly snapshot + report.

`python -m sieve_ingest health` creates/refreshes four views, inserts a
snapshot row (trend detection), and prints the report. Run it after each
ingest cycle (agent calls it) or standalone. Everything is idempotent.

Views:
  sieve.v_brain_health    — per table × domain_tag: n, url %, hi-conf %, stale
  sieve.v_registry_health — per source: overdue?, rules contributed, last run
  sieve.v_org_hygiene     — org-name variants that need aliasing
  sieve.v_trusted_rules   — the citation-grade subset (conf ≥ .85, attributed)
"""

from __future__ import annotations

import json
import logging

from . import db

log = logging.getLogger('ingest.health')

VIEWS_SQL = """
CREATE TABLE IF NOT EXISTS sieve.health_snapshots (
    taken_at timestamptz NOT NULL DEFAULT now(),
    metrics  jsonb NOT NULL
);

CREATE OR REPLACE VIEW sieve.v_brain_health AS
SELECT 'rules' AS tbl, domain_tag::text, count(*) AS n,
       round(100.0*count(*) FILTER (WHERE source_url IS NOT NULL AND source_url<>'')/count(*),1) AS url_pct,
       round(100.0*count(*) FILTER (WHERE confidence_score ~ '^[0-9]+(\\.[0-9]+)?$'
             AND confidence_score::numeric>=0.9)/count(*),1) AS hi_conf_pct,
       count(*) FILTER (WHERE last_verified IS NULL
             OR last_verified < now()-interval '90 days') AS stale_90d,
       count(*) FILTER (WHERE embedding IS NULL) AS missing_embedding
FROM sieve.rules WHERE status IS DISTINCT FROM 'deprecated' GROUP BY 2
UNION ALL
SELECT 'principles', domain_tag::text, count(*),
       round(100.0*count(*) FILTER (WHERE source_url IS NOT NULL AND source_url<>'')/count(*),1),
       NULL,
       count(*) FILTER (WHERE last_verified IS NULL),
       count(*) FILTER (WHERE embedding IS NULL)
FROM sieve.principles GROUP BY 2
UNION ALL
SELECT 'anti_patterns', domain_tag::text, count(*),
       round(100.0*count(*) FILTER (WHERE source_url IS NOT NULL AND source_url<>'')/count(*),1),
       NULL,
       count(*) FILTER (WHERE last_verified IS NULL),
       count(*) FILTER (WHERE embedding IS NULL)
FROM sieve.anti_patterns GROUP BY 2
UNION ALL
SELECT 'playbooks', domain_tag::text, count(*),
       round(100.0*count(*) FILTER (WHERE source_url IS NOT NULL AND source_url<>'')/count(*),1),
       NULL, NULL, NULL
FROM sieve.playbooks GROUP BY 2;

CREATE OR REPLACE VIEW sieve.v_registry_health AS
SELECT s.source_id, s.canonical_org, s.tier, s.crawl_cadence_days,
       s.last_crawled_at,
       (s.last_crawled_at IS NULL OR
        s.last_crawled_at < now()-(s.crawl_cadence_days*2||' days')::interval) AS overdue,
       (SELECT count(*) FROM sieve.rules r WHERE r.source_org=s.canonical_org) AS rules_from_org,
       s.enabled
FROM sieve.source_registry s;

CREATE OR REPLACE VIEW sieve.v_org_hygiene AS
SELECT lower(regexp_replace(source_org,
        '^(www\\.)|\\.(com|io|org|ai|co|dev)$','','g')) AS org_key,
       array_agg(DISTINCT source_org) AS variants,
       count(*) AS n
FROM sieve.rules
WHERE source_org IS NOT NULL AND source_org<>''
GROUP BY 1 HAVING count(DISTINCT source_org)>1 ORDER BY n DESC;

CREATE OR REPLACE VIEW sieve.v_trusted_rules AS
SELECT * FROM sieve.rules
WHERE status IS DISTINCT FROM 'deprecated'
  AND coalesce(rule_type,'') <> 'observed'   -- crawl-derived knowledge is never trusted-tier
  AND confidence_score ~ '^[0-9]+(\.[0-9]+)?$' AND confidence_score::numeric >= 0.85
  AND coalesce(source_org,'') NOT IN ('', 'Personal Blog', 'Unknown', 'unattributed-legacy');
"""

SNAPSHOT_SQL = """
SELECT jsonb_build_object(
  'rules_total',         (SELECT count(*) FROM sieve.rules WHERE status IS DISTINCT FROM 'deprecated'),
  'rules_url_pct',       (SELECT round(100.0*count(*) FILTER (WHERE source_url<>'' AND source_url IS NOT NULL)/count(*),1) FROM sieve.rules),
  'rules_trusted',       (SELECT count(*) FROM sieve.v_trusted_rules),
  'principles_total',    (SELECT count(*) FROM sieve.principles),
  'principles_url_pct',  (SELECT round(100.0*count(*) FILTER (WHERE source_url<>'' AND source_url IS NOT NULL)/count(*),1) FROM sieve.principles),
  'anti_patterns_total', (SELECT count(*) FROM sieve.anti_patterns),
  'ap_url_pct',          (SELECT round(100.0*count(*) FILTER (WHERE source_url<>'' AND source_url IS NOT NULL)/count(*),1) FROM sieve.anti_patterns),
  'aeo_geo_rules',       (SELECT count(*) FROM sieve.rules WHERE domain_tag::text IN ('aeo','geo')),
  'sources_overdue',     (SELECT count(*) FROM sieve.v_registry_health WHERE overdue),
  'rules_missing_embedding', (SELECT count(*) FROM sieve.rules WHERE embedding IS NULL),
  'last_run_status',     (SELECT status FROM sieve.ingest_runs ORDER BY run_id DESC LIMIT 1)
)
"""


def ensure_views(conn=None) -> None:
    own = conn is None
    conn = conn or db.connect()
    try:
        with conn.cursor() as cur:
            # CREATE OR REPLACE VIEW takes ACCESS EXCLUSIVE — never let a
            # long-running reader wedge the weekly cron (restartPolicy NEVER).
            cur.execute("SET lock_timeout='10s'; SET statement_timeout='120s'")
            cur.execute(VIEWS_SQL)
            cur.execute("SET lock_timeout=DEFAULT; SET statement_timeout=DEFAULT")
    finally:
        if own:
            conn.close()


def snapshot(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute(SNAPSHOT_SQL)
        metrics = cur.fetchone()[0]
        cur.execute("INSERT INTO sieve.health_snapshots (metrics) VALUES (%s)",
                    (json.dumps(metrics),))
        # previous snapshot for the delta line
        cur.execute("SELECT metrics FROM sieve.health_snapshots "
                    "ORDER BY taken_at DESC OFFSET 1 LIMIT 1")
        prev = cur.fetchone()
    return {'current': metrics, 'previous': (prev[0] if prev else None)}


def report(conn=None) -> dict:
    """Create views, snapshot, print human-readable report; returns metrics."""
    own = conn is None
    conn = conn or db.connect()
    try:
        ensure_views(conn)
        snap = snapshot(conn)
        cur_m, prev_m = snap['current'], snap['previous']
        print('=== SIEVE BRAIN HEALTH ===')
        for k in sorted(cur_m):
            delta = ''
            if prev_m and k in prev_m and isinstance(cur_m[k], (int, float)) \
                    and isinstance(prev_m.get(k), (int, float)):
                d = cur_m[k] - prev_m[k]
                if d:
                    delta = f'  ({"+" if d > 0 else ""}{d} vs last)'
            print(f'  {k:26s} {cur_m[k]}{delta}')
        with conn.cursor() as c2:
            c2.execute("SELECT source_id, tier, last_crawled_at::date, rules_from_org "
                       "FROM sieve.v_registry_health WHERE overdue ORDER BY tier")
            overdue = c2.fetchall()
        if overdue:
            print('\n  OVERDUE SOURCES:')
            for sid, tier, last, nrules in overdue:
                print(f'    T{tier} {sid:26s} last={last} rules={nrules}')
        alerts = []
        if cur_m.get('last_run_status') == 'degraded':
            alerts.append('last ingest run DEGRADED — check ingest_runs.detail')
        if prev_m:
            for k in ('rules_url_pct', 'principles_url_pct', 'ap_url_pct'):
                try:
                    if float(prev_m.get(k) or 0) - float(cur_m.get(k) or 0) > 2.0:
                        alerts.append(f'{k} dropped >2pts since last snapshot')
                except (TypeError, ValueError):
                    pass
        for a in alerts:
            print(f'  ⚠ ALERT: {a}')
        return snap
    finally:
        if own:
            conn.close()
