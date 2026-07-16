"""
verify/classify.py — Tier-1 DETERMINISTIC liveness classification.

Pure functions over an already-fetched HTTP result. This tier establishes
whether a rule's source_url is LIVE and WHEN the page last changed. It never
decides whether the rule is still TRUE (that is Tier-2 entailment) and it never
writes last_verified — a fetch must never masquerade as a re-verification.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Optional

# Link states (deterministic).
LIVE = "live"          # 2xx, page fetched
REDIRECT = "redirect"  # 3xx to a different canonical
DEAD = "dead"          # 4xx (gone / not found) — a broken proof link
PAUSED = "paused"      # 402 (e.g. Vercel deployment paused) — transient, not gone
BLOCKED = "blocked"    # 401/403 — bot-blocked, cannot confirm, NOT dead
DNS_FAIL = "dns_fail"  # host does not resolve
ERROR = "error"        # 5xx / timeout / transport — transient


def classify(status: Optional[int], requested_url: str,
             final_url: Optional[str], transport_error: Optional[str] = None) -> Dict[str, Any]:
    """Map an HTTP outcome to a link state. `status` is the HTTP code (None if
    the request never completed); `transport_error` distinguishes DNS from other
    transport failures. Returns {link_status, redirected, final_url}."""
    if status is None:
        te = (transport_error or "").lower()
        link = DNS_FAIL if ("name" in te or "dns" in te or "resolve" in te) else ERROR
        return {"link_status": link, "redirected": False, "final_url": None}
    if status == 402:
        link = PAUSED
    elif status in (401, 403):
        link = BLOCKED
    elif 200 <= status < 300:
        link = LIVE
    elif status in (301, 302, 303, 307, 308):
        link = REDIRECT
    elif status in (404, 410) or (400 <= status < 500):
        link = DEAD
    else:  # 5xx and anything else
        link = ERROR
    fu = final_url or requested_url
    return {"link_status": link, "redirected": bool(fu and fu != requested_url), "final_url": fu}


def content_hash(main_text: Optional[str]) -> Optional[str]:
    """Stable hash of the page's MAIN extracted text (whitespace-normalized), so
    an unchanged page short-circuits Tier-2 entailment. None if no text."""
    if not main_text:
        return None
    norm = re.sub(r"\s+", " ", main_text).strip().lower()
    return hashlib.md5(norm.encode("utf-8")).hexdigest() if norm else None


_DATE_META = re.compile(
    r'(?:article:modified_time|og:updated_time|dateModified)"?\s*[:=]\s*"?'
    r'(\d{4}-\d{2}-\d{2})', re.I)
_VISIBLE_DATE = re.compile(
    r'(?:updated|modified|last reviewed|reviewed on)[^0-9]{0,20}(\d{4}-\d{2}-\d{2})', re.I)


def last_modified_seen(headers: Optional[Dict[str, str]], html: Optional[str]) -> Optional[str]:
    """ADVISORY freshness signal (Last-Modified header / meta / visible date).
    Advisory only — it is NEVER written as last_verified (that requires Tier-2
    entailment); it just helps prioritize what to re-entail."""
    if headers:
        lm = headers.get("last-modified") or headers.get("Last-Modified")
        if lm:
            return lm
    if html:
        m = _DATE_META.search(html) or _VISIBLE_DATE.search(html)
        if m:
            return m.group(1)
    return None


def _selftest() -> None:
    assert classify(200, "u", "u")["link_status"] == LIVE
    assert classify(402, "u", "u")["link_status"] == PAUSED
    assert classify(403, "u", "u")["link_status"] == BLOCKED
    assert classify(404, "u", "u")["link_status"] == DEAD
    assert classify(410, "u", "u")["link_status"] == DEAD
    assert classify(500, "u", "u")["link_status"] == ERROR
    r = classify(301, "http://a", "https://b")
    assert r["link_status"] == REDIRECT and r["redirected"] is True and r["final_url"] == "https://b"
    assert classify(None, "u", None, transport_error="Name or service not known")["link_status"] == DNS_FAIL
    assert classify(None, "u", None, transport_error="read timeout")["link_status"] == ERROR
    # redirected flag only when final != requested
    assert classify(200, "https://x/p", "https://x/p")["redirected"] is False
    # content hash is stable under whitespace/case
    assert content_hash("Hello   World") == content_hash("hello world")
    assert content_hash("") is None and content_hash(None) is None
    # last_modified: header wins, then meta, then visible
    assert last_modified_seen({"last-modified": "Wed, 01 Jan 2026"}, None) == "Wed, 01 Jan 2026"
    assert last_modified_seen(None, 'article:modified_time":"2026-05-03"') == "2026-05-03"
    assert last_modified_seen(None, "Last reviewed on 2026-04-01 by Google") == "2026-04-01"
    assert last_modified_seen(None, "no date here") is None
    print("VERIFY_CLASSIFY_OK")


if __name__ == "__main__":
    _selftest()
