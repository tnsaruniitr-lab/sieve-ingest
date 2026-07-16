# Sieve Ingest Runbook — chat-driven enrichment (no API key)

**Mode (since 2026-07-16):** the Railway cron is MONITOR-ONLY (`MAX_URLS_PER_SOURCE=0`
service variable) — it runs weekly detection + `health` report but never calls the
Anthropic API. All rule extraction happens through a Claude Code chat session
(subscription tokens, not API dollars) via the file-bridge loop below.

## The enrichment loop (run in a Claude Code session, ~monthly or on demand)

Say: **"run the sieve enrichment loop"** — or manually:

```bash
cd sieve-ingest

# 1. HARVEST — fetch changed pages from all 21 sources (no LLM, no state writes)
railway run .venv-local/bin/python harvest_pages.py /tmp/harvest.jsonl 25

# 2. EXTRACT — Claude (in-chat subagents) turns pages into rules using the rubric
#    in the sieve-local-extraction workflow (scratchpad chunks → extracted_*.jsonl).
#    Rubric: atomic testable rules, verbatim technical values, max 8/page,
#    0.9+ confidence only for first-party normative statements.

# 3. INGEST — commit rules with full provenance + advance url_state/run log
railway run .venv-local/bin/python ingest_extracted.py /tmp/extracted_all.jsonl

# 4. EMBED — vectors for the new rules (needs a 1536-dim OpenAI key, e.g. loopr/.env)
OPENAI_API_KEY=... railway run .venv-local/bin/python embed_brain.py

# 5. VERIFY
railway run .venv-local/bin/python -m sieve_ingest critic   # 45-topic canon probe
railway run .venv-local/bin/python -m sieve_ingest health   # coverage + drift alerts
```

## Repair scripts (idempotent, re-run anytime; all write dated sieve.bak_* backups)

```bash
railway run .venv-local/bin/python normalize_orgs.py                      # org hygiene
railway run .venv-local/bin/python backfill_urls.py --table rules         # URL adoption
railway run .venv-local/bin/python backfill_urls.py --table principles
railway run .venv-local/bin/python backfill_urls.py --table anti_patterns
```

## Notes
- To re-enable API-key extraction on the cron: set `MAX_URLS_PER_SOURCE=25` in the
  Railway service variables (the hardened code fails loudly on a bad key and never
  burns pages on transient failures).
- Add a source = add a row to `SEED_SOURCES` in `sieve_ingest/registry.py`, then
  `railway run .venv-local/bin/python -m sieve_ingest seed`.
- Local venv: `.venv-local/` (python3 -m venv .venv-local && pip install -r requirements.txt).
- DB access pattern: `railway run bash -lc 'psql "$SIEVE_DB_URL" ...'` (retry on
  transient "Failed to fetch" — flaky local network).
