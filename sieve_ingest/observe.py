"""
observe.py — the flywheel: crawl-derived OBSERVATIONS become candidate rules.

    python -m sieve_ingest observe <observations.jsonl> [--dry-run]

AnswerMonk (or any crawler) emits deterministic prevalence observations —
"N of M AI-cited pages in this market show <feature>, across S sessions" —
as JSONL. This command writes them into sieve.rules through the same
provenance path the ingest uses, with hard guardrails so crawl knowledge
NEVER masquerades as an authority norm:

  * rule_type='observed', status='candidate'  (retrievable, never trusted)
  * source_org='AnswerMonk Crawl'             (tier 5 in every consumer)
  * confidence = min(0.84, 0.5 + 0.4*prevalence)  — capped BELOW the 0.85
    v_trusted_rules floor (and the view additionally excludes rule_type
    'observed'; belt and braces)
  * url_provenance = {method:'observed-crawl', prevalence, sessions, ...}

Input line shape (all required unless noted):
  {"name": "...", "if_condition": "...", "then_logic": "...",
   "domain_tag": "aeo",
   "prevalence": {"n": 8, "m": 10, "sessions": 3},
   "exemplar_url": "https://...",          # optional — evidence, not authority
   "observer": "answermonk-fable5",        # optional
   "session_ids": ["..."]}                 # optional

Acceptance gates (rejected lines are reported, not written):
  prevalence.n/m >= MIN_PREVALENCE (default 0.7) and sessions >= MIN_SESSIONS
  (default 3). Re-observation of an existing rule_key refreshes last_verified
  via the standard upsert path.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

from . import db

log = logging.getLogger('ingest.observe')

OBSERVED_ORG = 'AnswerMonk Crawl'
CONF_CAP = 0.84                     # must stay < v_trusted_rules' 0.85 floor
MIN_PREVALENCE = float(os.getenv('OBSERVE_MIN_PREVALENCE', '0.7'))
MIN_SESSIONS = int(os.getenv('OBSERVE_MIN_SESSIONS', '3'))
VALID_TAGS = {'seo', 'aeo', 'geo', 'entity', 'content', 'performance', 'general'}


def _validate(obs: dict):
    """Returns (rule_dict, prevalence_dict) or (None, reason)."""
    name = str(obs.get('name') or '').strip()
    if_c = str(obs.get('if_condition') or '').strip()
    then = str(obs.get('then_logic') or '').strip()
    if not (name and if_c and then):
        return None, 'missing name/if_condition/then_logic'
    prev = obs.get('prevalence') or {}
    try:
        n, m, s = int(prev['n']), int(prev['m']), int(prev.get('sessions', 0))
    except (KeyError, TypeError, ValueError):
        return None, 'missing/invalid prevalence {n,m,sessions}'
    if m <= 0 or n < 0 or n > m:
        return None, f'implausible prevalence {n}/{m}'
    ratio = n / m
    if ratio < MIN_PREVALENCE:
        return None, f'prevalence {ratio:.0%} below {MIN_PREVALENCE:.0%} gate'
    if s < MIN_SESSIONS:
        return None, f'{s} sessions below {MIN_SESSIONS} gate'
    tag = str(obs.get('domain_tag') or 'general').lower()
    if tag not in VALID_TAGS:
        tag = 'general'
    conf = round(min(CONF_CAP, 0.5 + 0.4 * ratio), 2)
    rule = {'name': name[:300], 'if_condition': if_c, 'then_logic': then,
            'domain_tag': tag, 'confidence_score': conf, 'rule_type': 'observed'}
    return (rule, {'n': n, 'm': m, 'sessions': s, 'ratio': round(ratio, 3)}), None


def run(path: str, dry_run: bool = False) -> dict:
    counts = {'lines': 0, 'new': 0, 'refreshed': 0, 'rejected': 0}
    rejects = []
    conn = None if dry_run else db.connect()
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                counts['lines'] += 1
                try:
                    obs = json.loads(line)
                except Exception:
                    counts['rejected'] += 1
                    rejects.append('unparseable line')
                    continue
                ok, reason = _validate(obs)
                if ok is None:
                    counts['rejected'] += 1
                    rejects.append(f"{(obs.get('name') or '?')[:50]}: {reason}")
                    continue
                rule, prev = ok
                if dry_run:
                    counts['new'] += 1
                    print(f"  DRY {rule['name'][:60]}  conf={rule['confidence_score']} "
                          f"prevalence={prev['n']}/{prev['m']}@{prev['sessions']}s")
                    continue
                prov = {'method': 'observed-crawl',
                        'at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                        'prevalence': prev,
                        'observer': obs.get('observer', 'unknown'),
                        'exemplars': ([obs['exemplar_url']] if obs.get('exemplar_url') else []),
                        'session_ids': (obs.get('session_ids') or [])[:20]}
                doc_id = db.upsert_document(
                    conn, source_url=obs.get('exemplar_url') or '',
                    source_org=OBSERVED_ORG,
                    title=f"Observed pattern: {rule['name']}"[:200],
                    domain_tag=rule['domain_tag'])
                outcome = db.upsert_rule(conn, rule, doc_id=doc_id,
                                         source_url=obs.get('exemplar_url') or '',
                                         source_org=OBSERVED_ORG,
                                         status='candidate', url_provenance=prov)
                counts[outcome] = counts.get(outcome, 0) + 1
                print(f"  {outcome.upper():9s} {rule['name'][:60]}  "
                      f"conf={rule['confidence_score']} prevalence={prev['n']}/{prev['m']}")
    finally:
        if conn:
            conn.close()
    if rejects:
        print(f"\nrejected {len(rejects)}:")
        for r in rejects[:10]:
            print(f"  - {r}")
    print(f"\nDONE: {counts}")
    return counts
