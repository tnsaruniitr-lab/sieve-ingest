"""Rule verification + freshness harness (Tier-1 deterministic, Tier-2 entailment).

The corpus is WRITTEN only from sieve-ingest — the auditor and answermonk are
read-only over sieve. This subpackage earns honest freshness so a citation can
say 'verified <date>' only when the source page was re-read and still supports
the rule. See classify (T1) and writer (the honesty invariant).
"""
