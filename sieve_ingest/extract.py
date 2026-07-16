"""
extract.py — turn a changed page into brain rules, with provenance stamped.

Focused LLM extraction: given the page text + the source's canonical org + URL,
pull out atomic SEO/AEO/GEO *rules* (if_condition → then_logic) and write them to
sieve.rules via db.upsert_rule (which dedupes by rule_key and refreshes
last_verified instead of duplicating). Each rule carries source_org + source_url
+ document_id + extracted_at + last_verified.

This is the provenance-preserving extraction stage. It is deliberately smaller
than the full ILD LangGraph (no embeddings here — embed_brain.py backfills
vectors separately), but it keeps the exact same brain-object shape so the
auditor reads new rules identically.

Hardened (2026-07-16): balanced-bracket JSON parsing (greedy-regex removed),
truncated-array salvage, per-rule schema validation with confidence clamping,
and an explicit status channel so a failed extraction is distinguishable from
a legitimately rule-free page (agent.py records statuses in ingest_runs.detail).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Dict, List, Tuple

from . import db

log = logging.getLogger('ingest.extract')

MODEL = os.getenv('INGEST_MODEL', 'claude-sonnet-4-6')
MAX_RULES_PER_PAGE = int(os.getenv('MAX_RULES_PER_PAGE', '8'))
MAX_EXTRACT_TOKENS = int(os.getenv('MAX_EXTRACT_TOKENS', '4000'))
MAX_TEXT_CHARS = int(os.getenv('MAX_TEXT_CHARS', '12000'))

VALID_DOMAIN_TAGS = {'seo', 'aeo', 'geo', 'entity', 'content', 'performance', 'general'}

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
and anything not a testable directive. Preserve technical values (bot names,
thresholds, tag/property names) verbatim from the source. If the text has no
real rules, return [].

TEXT:
{text}
"""


# ---------------------------------------------------------------------------
# Robust JSON parsing
# ---------------------------------------------------------------------------

def _scan_array(raw: str, start: int) -> str | None:
    """Return the balanced JSON array starting at raw[start], honoring strings
    and escapes. None if the array never closes (truncated output)."""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(raw)):
        c = raw[i]
        if in_str:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c in '[{':
            depth += 1
        elif c in ']}':
            depth -= 1
            if depth == 0:
                return raw[start:i + 1]
    return None


def _salvage_truncated(raw: str, start: int) -> str | None:
    """max_tokens can cut the array mid-object. Recover the complete objects:
    trim to the last fully-closed '}' and close the array."""
    last_obj_end = raw.rfind('}')
    if last_obj_end <= start:
        return None
    return raw[start:last_obj_end + 1] + ']'


def _parse_rules(raw: str):
    """Parse the LLM output into a list of rule dicts.
    Returns a list on success ([] = the model genuinely said "no rules"),
    or None when the output was unparseable. Tries, in order: whole-string
    parse, each balanced array in the text, truncation salvage."""
    raw = (raw or '').strip()
    if not raw:
        return None
    # 1) the whole response is the array (the common, prompt-compliant case)
    try:
        val = json.loads(raw)
        if isinstance(val, list):
            return val
    except Exception:
        pass
    # 2) balanced-scan from each '[' — immune to prose before/after and to
    #    later bracketed asides that broke the old greedy regex. An EMPTY
    #    array candidate is remembered but scanning continues: a literal []
    #    in prose/code examples must not shadow the real rules array.
    saw_empty = False
    idx = raw.find('[')
    while idx != -1:
        candidate = _scan_array(raw, idx)
        if candidate:
            try:
                val = json.loads(candidate)
                if isinstance(val, list):
                    if val and isinstance(val[0], dict):
                        return val
                    if not val:
                        saw_empty = True
            except Exception:
                pass
        else:
            # 3) array never closed → truncated output; salvage complete objects
            salvaged = _salvage_truncated(raw, idx)
            if salvaged:
                try:
                    val = json.loads(salvaged)
                    if isinstance(val, list) and val:
                        log.warning('salvaged %d rules from truncated output', len(val))
                        return val
                except Exception:
                    pass
        idx = raw.find('[', idx + 1)
    return [] if saw_empty else None


def _valid_rule(r) -> Dict | None:
    """Schema-validate one extracted rule; normalize in place. None = drop."""
    if not isinstance(r, dict):
        return None
    name = str(r.get('name') or '').strip()
    if_cond = str(r.get('if_condition') or '').strip()
    then_logic = str(r.get('then_logic') or '').strip()
    if not name or not if_cond or not then_logic:
        return None
    tag = str(r.get('domain_tag') or 'general').strip().lower()
    if tag not in VALID_DOMAIN_TAGS:
        tag = 'general'
    try:
        conf = float(r.get('confidence_score', 0.8))
    except (TypeError, ValueError):
        conf = 0.8
    conf = max(0.0, min(1.0, conf))
    return {'name': name[:300], 'if_condition': if_cond, 'then_logic': then_logic,
            'domain_tag': tag, 'confidence_score': round(conf, 2)}


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def _extract_rules(text: str, org: str, url: str) -> Tuple[List[Dict], str]:
    """Returns (validated_rules, status). status distinguishes failure classes
    from a legitimately empty page:
      ok | empty | no_sdk | no_api_key | llm_error | parse_error | all_invalid
    """
    try:
        from anthropic import Anthropic
    except Exception:
        log.warning('anthropic SDK unavailable — skipping extraction')
        return [], 'no_sdk'
    key = os.getenv('ANTHROPIC_API_KEY')
    if not key:
        log.warning('ANTHROPIC_API_KEY not set — skipping extraction')
        return [], 'no_api_key'
    client = Anthropic(api_key=key)
    prompt = _PROMPT.format(org=org, url=url, maxr=MAX_RULES_PER_PAGE,
                            text=text[:MAX_TEXT_CHARS])
    try:
        resp = client.messages.create(model=MODEL, max_tokens=MAX_EXTRACT_TOKENS,
                                      messages=[{'role': 'user', 'content': prompt}])
        raw = ''.join(b.text for b in resp.content if getattr(b, 'type', None) == 'text')
    except Exception as e:
        log.warning('extraction LLM call failed for %s: %s', url, e)
        return [], 'llm_error'

    parsed = _parse_rules(raw)
    if parsed is None:
        log.warning('unparseable extraction output for %s (%d chars)', url, len(raw))
        return [], 'parse_error'
    if not parsed:
        return [], 'empty'

    valid = []
    for r in parsed[:MAX_RULES_PER_PAGE]:
        v = _valid_rule(r)
        if v:
            valid.append(v)
    if parsed and not valid:
        return [], 'all_invalid'
    if len(valid) < len(parsed[:MAX_RULES_PER_PAGE]):
        log.warning('dropped %d invalid rule dicts for %s',
                    len(parsed[:MAX_RULES_PER_PAGE]) - len(valid), url)
    return valid, 'ok'


# ---------------------------------------------------------------------------
# Page ingestion
# ---------------------------------------------------------------------------

def _fetch_text(url: str) -> str:
    """On-demand fetch for adapters that signal change without carrying text
    (github_release). Best effort; empty string on failure."""
    try:
        import httpx
        from .freshness import UA, _main_text
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            r = client.get(url, headers=UA)
            if r.status_code == 200:
                return _main_text(r.text)
    except Exception as e:
        log.warning('on-demand fetch failed for %s: %s', url, e)
    return ''


def ingest_page(conn, changed, source) -> Dict:
    """Extract rules from one changed page and write them with provenance.
    Returns {new, refreshed, dropped, status}."""
    text = changed.text
    if not text:  # changelog/github adapters may not carry text; fetch on demand
        text = _fetch_text(changed.url)
    if not text:
        return {'new': 0, 'refreshed': 0, 'dropped': 0, 'status': 'no_text'}
    org = source['canonical_org']
    url = changed.url

    rules, status = _extract_rules(text, org, url)
    if not rules:
        return {'new': 0, 'refreshed': 0, 'dropped': 0, 'status': status}

    # Document tag = majority vote across extracted rules (not rules[0]);
    # title = the page's own <title> when freshness captured it.
    tag_counts: Dict[str, int] = {}
    for r in rules:
        tag_counts[r['domain_tag']] = tag_counts.get(r['domain_tag'], 0) + 1
    doc_tag = max(tag_counts, key=tag_counts.get)
    page_title = (getattr(changed, 'title', '') or rules[0]['name'] or url)[:200]

    doc_id = db.upsert_document(conn, source_url=url, source_org=org,
                                title=page_title, domain_tag=doc_tag)
    counts = {'new': 0, 'refreshed': 0, 'dropped': 0, 'status': status}
    for r in rules:
        try:
            outcome = db.upsert_rule(conn, r, doc_id=doc_id, source_url=url, source_org=org)
            counts[outcome] = counts.get(outcome, 0) + 1
        except Exception as e:
            counts['dropped'] += 1
            log.warning('rule write failed for %s: %s', url, e)
    if counts['dropped'] and not counts['new'] and not counts['refreshed']:
        # every write failed → schema drift / DB problem, not a page problem
        counts['status'] = 'write_error'
    log.info('  %s → %d new / %d refreshed / %d dropped rules',
             url, counts['new'], counts['refreshed'], counts['dropped'])
    return counts
