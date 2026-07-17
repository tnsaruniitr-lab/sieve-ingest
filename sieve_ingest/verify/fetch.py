"""
verify/fetch.py — Tier-1 network fetch (reuses the ingest httpx stack).

Fetches a URL, extracts main text (trafilatura), and hands the raw outcome to
classify.py. Conditional-GET aware (ETag/If-Modified-Since) so a re-run over the
whole corpus is mostly 304s. No Playwright here — server-rendered doc pages
(most of Google/Moz/W3C) work with httpx; JS-only pages are a later add via a
headless render fallback. Never raises; returns a structured result.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

try:
    from . import classify as C
except ImportError:
    import classify as C

_UA = ("Mozilla/5.0 (compatible; sieve-verify/1.0; +https://sieve.local/verify) "
       "AppleWebKit/537.36")


def fetch(url: str, etag: Optional[str] = None, last_modified: Optional[str] = None,
          timeout: float = 20.0) -> Dict[str, Any]:
    """Fetch one URL. Returns:
      {link_status, http_status, final_url, redirected, main_text, content_hash,
       last_modified_seen, etag, unchanged(bool), error}
    `unchanged` is True on a 304 (conditional GET hit) — caller skips re-entail.
    """
    import httpx
    headers = {"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    try:
        with httpx.Client(follow_redirects=True, timeout=timeout, headers=headers) as client:
            resp = client.get(url)
    except httpx.ConnectError as e:
        cl = C.classify(None, url, None, transport_error=str(e))
        return {**cl, "http_status": None, "main_text": None, "content_hash": None,
                "last_modified_seen": None, "etag": None, "unchanged": False,
                "error": f"connect: {e}"}
    except Exception as e:  # timeout / transport
        cl = C.classify(None, url, None, transport_error=str(e))
        return {**cl, "http_status": None, "main_text": None, "content_hash": None,
                "last_modified_seen": None, "etag": None, "unchanged": False,
                "error": f"{type(e).__name__}: {e}"}

    status = resp.status_code
    final_url = str(resp.url)
    cl = C.classify(status, url, final_url)

    if status == 304:  # conditional GET: unchanged
        return {**cl, "link_status": C.LIVE, "http_status": 304, "main_text": None,
                "content_hash": None, "last_modified_seen": None,
                "etag": resp.headers.get("etag"), "unchanged": True, "error": None}

    main_text = None
    if cl["link_status"] == C.LIVE:
        try:
            import trafilatura
            main_text = trafilatura.extract(resp.text) or None
        except Exception:
            main_text = None

    return {
        **cl,
        "http_status": status,
        "main_text": main_text,
        "content_hash": C.content_hash(main_text),
        "last_modified_seen": C.last_modified_seen(dict(resp.headers), resp.text[:20000]),
        "etag": resp.headers.get("etag"),
        "unchanged": False,
        "error": None,
    }
