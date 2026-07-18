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
import re
from typing import Dict, List, Tuple

from . import db

log = logging.getLogger('ingest.extract')

MODEL = os.getenv('INGEST_MODEL', 'claude-sonnet-4-6')
MAX_RULES_PER_PAGE = int(os.getenv('MAX_RULES_PER_PAGE', '8'))
MIN_RULE_CONFIDENCE = float(os.getenv('MIN_RULE_CONFIDENCE', '0.6'))
CHUNK_CHARS = 12000  # per-LLM-call text budget; long docs get a second chunk
MAX_EXTRACT_TOKENS = int(os.getenv('MAX_EXTRACT_TOKENS', '4000'))  # 2000 truncated dense pages


class ExtractError(Exception):
    """Extraction FAILED (SDK missing, no API key, API error, unparseable LLM
    output) — as opposed to a genuine 'this page has no rules' empty result.
    The caller must NOT consume the change (no url_state save) so the same
    content version is retried next cycle instead of being lost forever."""


# Cheap relevance screen for sitemap-discovered pages (curated url_list seeds
# skip it). The Jul-6 run extracted 39 "rules" from a CSS-masking article and 21
# from MDN site chrome — a page with none of these terms is not worth a Sonnet
# call. Deliberately broad (a false PASS costs one LLM call; a false skip loses
# the page until its content changes), so short tokens are word-bounded and the
# list errs toward inclusion.
_RELEVANCE_RE = re.compile(
    r'seo\b|search engine|search ranking|google search|googlebot|bingbot'
    r'|crawl|index(?:ing|ation)|structured data|schema\.org|json-ld'
    r'|sitemap|robots\.txt|meta description|title tag|\bsnippets?\b'
    r'|canonical|core web vitals|page experience|\blcp\b|\binp\b|\bcls\b'
    r'|page ?speed|lighthouse|mobile-friendly|ranking|\bserp\b'
    r'|redirects?\b|rich results?|alt (?:text|attribute)|open graph|\bog:'
    r'|answer engine|ai overview|featured snippet|knowledge (?:graph|panel)'
    r'|llms?\.txt|gptbot|ai crawler|citation|e-?e-?a-?t|hreflang|backlink',
    re.I)

_PROMPT = """You extract atomic, testable SEO/AEO/GEO RULES from documentation text.

Source: {org} — {url}

Return ONLY a JSON array (max {maxr}) of rules, each:
{{"name": "<short imperative title>",
  "if_condition": "<the situation the rule applies to>",
  "then_logic": "<the recommended action>",
  "domain_tag": "seo|aeo|geo|entity|content|performance|general",
  "confidence_score": 0.0-1.0,
  "status": "active|deprecated"}}

Rules must be concrete and page-checkable (e.g. "Use JSON-LD for structured
data", "Author needs hasCredential for YMYL"). Skip marketing fluff, opinions,
and anything not a testable directive. Preserve technical values (bot names,
thresholds, tag/property names) verbatim from the source. If the text has no
real rules, return [].

Deprecation: guidance the source ITSELF marks as deprecated, retired, or
sunset (e.g. "HowTo rich results are deprecated") must still be emitted, but
with "status": "deprecated" — never present it as current advice. Everything
else is "status": "active" (the default if omitted).

TEXT:
{text}
"""

# Quality-screen backstop for guidance the platforms have deprecated but pages
# still describe as if current ("HowTo rich result" style claims — deprecated
# by Google 2023-08, FAQ rich results restricted at the same time). The LLM is
# INSTRUCTED to mark these (see _PROMPT); this deterministic screen catches the
# ones it emits as active — flagged onto the deprecated path, never silently
# kept as current guidance.
_DEPRECATED_RE = re.compile(
    r'how-?to rich results?|how-?to (?:schema|markup|structured data)'
    r'|faq(?:page)? rich results?', re.I)


def _salvage_array(raw: str):
    """Recover a rule array the cheap parse missed: max_tokens can cut the
    array mid-object, and the greedy regex over-captures when prose follows.
    Balanced string-aware scan from the first '['; failing that, trim to the
    last fully-closed '}' and close the array (keeps the complete objects).
    Returns a list or None — never raises."""
    start = raw.find('[')
    if start < 0:
        return None
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
                try:
                    v = json.loads(raw[start:i + 1])
                    return v if isinstance(v, list) else None
                except Exception:
                    break
    last = raw.rfind('}')
    if last > start:
        try:
            v = json.loads(raw[start:last + 1] + ']')
            return v if isinstance(v, list) else None
        except Exception:
            return None
    return None


def _extract_rules(text: str, org: str, url: str) -> List[Dict]:
    """Returns the extracted rules ([] = the page genuinely has none).
    Raises ExtractError on any failure — never masks a failure as 'no rules'."""
    try:
        from anthropic import Anthropic
    except Exception as e:
        raise ExtractError(f'anthropic SDK unavailable: {e}')
    key = os.getenv('ANTHROPIC_API_KEY')
    if not key:
        raise ExtractError('ANTHROPIC_API_KEY not set')
    client = Anthropic(api_key=key)
    prompt = _PROMPT.format(org=org, url=url, maxr=MAX_RULES_PER_PAGE, text=text)
    try:
        resp = client.messages.create(model=MODEL, max_tokens=MAX_EXTRACT_TOKENS,
                                      messages=[{'role': 'user', 'content': prompt}])
        raw = ''.join(b.text for b in resp.content if getattr(b, 'type', None) == 'text')
    except Exception as e:
        raise ExtractError(f'LLM call failed: {e}')
    m = re.search(r'\[.*\]', raw, re.S)
    if not m:
        rules = _salvage_array(raw)
        if rules is None:
            raise ExtractError('no JSON array in LLM output')
    else:
        try:
            rules = json.loads(m.group(0))
        except Exception as e:
            rules = _salvage_array(raw)
            if rules is None:
                raise ExtractError(f'unparseable LLM JSON: {e}')
    if not isinstance(rules, list):
        raise ExtractError('LLM output is not a list')
    if not all(isinstance(r, dict) for r in rules):
        raise ExtractError('LLM output contains non-object rules')
    return rules


def _validate_rules(rules: List[Dict], url: str) -> tuple:
    """Write-time quality gate: required fields present + numeric confidence
    above MIN_RULE_CONFIDENCE. Returns (kept, rejected_count) — rejections are
    counted in the change record, never silently dropped. Kept rules whose text
    matches a known-deprecated claim (_DEPRECATED_RE) are flagged
    status='deprecated' here when the LLM emitted them as active."""
    kept, rejected, flagged = [], 0, 0
    for r in rules:
        try:
            conf = float(r.get('confidence_score', 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        if (r.get('name') and r.get('if_condition') and r.get('then_logic')
                and conf >= MIN_RULE_CONFIDENCE):
            r['confidence_score'] = conf
            if _rule_status(r) != 'deprecated' and _DEPRECATED_RE.search(
                    f"{r.get('name')} {r.get('if_condition')} {r.get('then_logic')}"):
                r['status'] = 'deprecated'
                flagged += 1
            kept.append(r)
        else:
            rejected += 1
    if rejected:
        log.info('  %s — %d rule(s) rejected by quality gate', url, rejected)
    if flagged:
        log.info('  %s — %d rule(s) flagged deprecated by the screen', url, flagged)
    return kept, rejected


def _rule_status(r: Dict) -> str:
    """Normalize the LLM's status field: deprecated/retired/sunset all land on
    the 'deprecated' path; anything else (including absent) is 'active'."""
    s = str(r.get('status') or '').strip().lower()
    return 'deprecated' if s in ('deprecated', 'retired', 'sunset') else 'active'


def _chunks(text: str) -> List[str]:
    """Long canonical docs (Google's starter guide) lose everything past the
    old 12k truncation — split at a paragraph boundary into at most 2 chunks."""
    if len(text) <= CHUNK_CHARS:
        return [text]
    cut = text.rfind('\n', 0, CHUNK_CHARS)
    cut = cut if cut > CHUNK_CHARS // 2 else CHUNK_CHARS
    return [text[:cut], text[cut:cut + CHUNK_CHARS]]


def ingest_page(conn, changed, source) -> Dict:
    """Extract rules from one changed page and write them with provenance.
    Returns {new, refreshed, status} where status is:
        extracted  — LLM ran, rules written (possibly 0 new if all deduped)
        empty      — nothing to extract (no text, or LLM found no rules)
        irrelevant — relevance screen skipped the page (no LLM spend)
        failed     — extraction errored; the change must NOT be consumed
    """
    text = changed.text
    if not text:  # changelog/github adapters may not carry text; fetch on demand
        return {'new': 0, 'refreshed': 0, 'status': 'empty'}
    org = source['canonical_org']
    url = changed.url

    # Relevance screen for sitemap-discovered pages only — url_list seeds are
    # curated exact pages and always go to extraction. TWO distinct signal terms
    # required: one leaks badly (MDN game-dev docs matched on 'indexing',
    # marketing posts on a lone 'seo' — 120 junk rules in the Jul-11 backfill).
    if source.get('adapter_type') == 'sitemap':
        hits = {m.group(0).lower() for m in _RELEVANCE_RE.finditer(text)}
        if len(hits) < 2:
            log.info('  %s — %d SEO/AEO signal term(s), skipping extraction',
                     url, len(hits))
            return {'new': 0, 'refreshed': 0, 'status': 'irrelevant'}

    try:
        rules = []
        for chunk in _chunks(text):
            rules.extend(_extract_rules(chunk, org, url))
    except ExtractError as e:
        log.warning('  %s extraction failed: %s', url, e)
        return {'new': 0, 'refreshed': 0, 'status': 'failed'}
    rules, rejected = _validate_rules(rules, url)
    if not rules:
        return {'new': 0, 'refreshed': 0, 'status': 'empty', 'rejected': rejected}

    doc_id = db.upsert_document(conn, source_url=url, source_org=org,
                               title=(rules[0].get('name') or url)[:200],
                               domain_tag=rules[0].get('domain_tag', 'general'))
    counts = {'new': 0, 'refreshed': 0, 'status': 'extracted', 'rejected': rejected}
    write_errors = 0
    for r in rules:
        try:
            outcome = db.upsert_rule(conn, r, doc_id=doc_id, source_url=url,
                                     source_org=org, status=_rule_status(r))
            counts[outcome] = counts.get(outcome, 0) + 1
        except Exception as e:
            write_errors += 1
            log.warning('rule write failed: %s', e)
    if write_errors:
        # Rules were extracted but not all landed — do not consume the change;
        # the retry re-extracts and upsert_rule dedup makes the replay idempotent.
        log.warning('  %s: %d/%d rule writes failed — not consuming', url,
                    write_errors, len(rules))
        counts['status'] = 'failed'
    log.info('  %s → %d new / %d refreshed rules', url, counts['new'], counts['refreshed'])
    return counts
