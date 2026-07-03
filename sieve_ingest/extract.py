"""
extract.py — turn a changed page into brain rules, with provenance stamped.

Focused LLM extraction: given the page text + the source's canonical org + URL,
pull out atomic SEO/AEO/GEO *rules* (if_condition → then_logic) and write them to
sieve.rules via db.upsert_rule (which dedupes by rule_key and refreshes
last_verified instead of duplicating). Each rule carries source_org + source_url
+ document_id + extracted_at + last_verified.

This is the provenance-preserving extraction stage. It is deliberately smaller
than the full ILD LangGraph (no embeddings here — the auditor's live retrieval
is FTS today; the vector pass is a separate upgrade), but it keeps the exact
same brain-object shape so the auditor reads new rules identically.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Dict, List

from . import db

log = logging.getLogger('ingest.extract')

MODEL = os.getenv('INGEST_MODEL', 'claude-sonnet-4-6')
MAX_RULES_PER_PAGE = int(os.getenv('MAX_RULES_PER_PAGE', '8'))

_PROMPT = """You extract atomic, testable SEO/AEO/GEO RULES from documentation text.

Source: {org} — {url}

Return ONLY a JSON array (max {maxr}) of rules, each:
{{"name": "<short imperative title>",
  "if_condition": "<the situation the rule applies to>",
  "then_logic": "<the recommended action>",
  "domain_tag": "seo|aeo|geo|entity|content|performance|general",
  "confidence_score": 0.0-1.0}}

Rules must be concrete and page-checkable (e.g. "Use JSON-LD for structured
data", "Author needs hasCredential for YMYL"). Skip marketing fluff, opinions,
and anything not a testable directive. If the text has no real rules, return [].

TEXT:
{text}
"""


def _extract_rules(text: str, org: str, url: str) -> List[Dict]:
    try:
        from anthropic import Anthropic
    except Exception:
        log.warning('anthropic SDK unavailable — skipping extraction'); return []
    key = os.getenv('ANTHROPIC_API_KEY')
    if not key:
        log.warning('ANTHROPIC_API_KEY not set — skipping extraction'); return []
    client = Anthropic(api_key=key)
    prompt = _PROMPT.format(org=org, url=url, maxr=MAX_RULES_PER_PAGE, text=text[:12000])
    try:
        resp = client.messages.create(model=MODEL, max_tokens=2000,
                                       messages=[{'role': 'user', 'content': prompt}])
        raw = ''.join(b.text for b in resp.content if getattr(b, 'type', None) == 'text')
    except Exception as e:
        log.warning('extraction LLM call failed: %s', e); return []
    m = re.search(r'\[.*\]', raw, re.S)
    if not m:
        return []
    try:
        rules = json.loads(m.group(0))
        return rules if isinstance(rules, list) else []
    except Exception:
        return []


def ingest_page(conn, changed, source) -> Dict[str, int]:
    """Extract rules from one changed page and write them with provenance.
    Returns {new, refreshed}."""
    text = changed.text
    if not text:  # changelog/github adapters may not carry text; fetch on demand
        return {'new': 0, 'refreshed': 0}
    org = source['canonical_org']
    url = changed.url
    domain_tag = 'general'

    rules = _extract_rules(text, org, url)
    if not rules:
        return {'new': 0, 'refreshed': 0}

    doc_id = db.upsert_document(conn, source_url=url, source_org=org,
                               title=(rules[0].get('name') or url)[:200],
                               domain_tag=rules[0].get('domain_tag', domain_tag))
    counts = {'new': 0, 'refreshed': 0}
    for r in rules:
        try:
            outcome = db.upsert_rule(conn, r, doc_id=doc_id, source_url=url, source_org=org)
            counts[outcome] = counts.get(outcome, 0) + 1
        except Exception as e:
            log.warning('rule write failed: %s', e)
    log.info('  %s → %d new / %d refreshed rules', url, counts['new'], counts['refreshed'])
    return counts
