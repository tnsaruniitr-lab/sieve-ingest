# sieve-ingest — the freshness / ingestion agent

A **separate scheduled worker** (not the auditor) that keeps the Sieve brain
current. Runs on a Railway cron; writes to the same central Postgres the auditor
reads (`sieve` schema).

## One cycle (`python -m sieve_ingest run`)
1. Seed the **source registry** (13 canonical sources, tiered, with cadence).
2. For each **due** source (cadence elapsed), **detect what changed** —
   cheapest signal first:
   - `github_release` → Schema.org release tag (one request)
   - `changelog` → hash Google's "what's new" page
   - `sitemap` → `<lastmod>` diff → conditional GET (ETag/304) → content-hash
3. For each changed URL, **extract rules with Claude** and write them to
   `sieve.rules` with `source_org + source_url + document_id + extracted_at +
   last_verified`. Dedupes by `rule_key` (refreshes `last_verified` instead of
   duplicating). Never hard-deletes.
4. Record the run + every change in `sieve.ingest_runs` / `sieve.ingest_changes`.

## Commands
    python -m sieve_ingest seed      # schema + registry
    python -m sieve_ingest run       # one cycle (Railway cron target)
    python -m sieve_ingest status    # registry + last runs
    python -m sieve_ingest changes   # recent detected changes

## Env
    SIEVE_DB_URL (or DATABASE_URL)   central Postgres (sieve schema)
    ANTHROPIC_API_KEY                 for rule extraction
    MAX_URLS_PER_SOURCE=15  MAX_RULES_PER_PAGE=8

## Deploy (Railway cron)
`railway.json` sets `cronSchedule: "0 6 * * 1"` (Mondays 06:00 UTC) with
`restartPolicyType: NEVER` — it runs, ingests, exits. Point the service at the
same Postgres as the auditor and set `ANTHROPIC_API_KEY`.
