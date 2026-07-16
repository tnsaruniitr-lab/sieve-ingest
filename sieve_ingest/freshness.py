"""
freshness.py — "what changed?" detection. Cheapest signal first.

Per source, we pick the RIGHT change signal (adapter_type) rather than blindly
re-scraping:

  github_release  → compare the latest release tag (Schema.org). One request.
  changelog       → hash the documented "what's new" page (Google). One request.
  sitemap         → diff each URL's <lastmod>; then per-URL:
                       1. conditional GET (ETag / If-Modified-Since) → 304 = skip
                       2. content hash (sha256 of normalized text) → definitive

Returns a list of ChangedURL for the extractor to process. Unchanged pages are
skipped BEFORE the expensive fetch/LLM step — that's the whole point.

stdlib + httpx.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import List, Optional

import httpx

from . import db

log = logging.getLogger('ingest.freshness')
UA = {'User-Agent': 'sieve-ingest/1.0 (+freshness-bot)'}


@dataclass
class ChangedURL:
    url: str
    change_type: str          # new | modified
    signal: str               # lastmod | etag | content_hash | version
    old_hash: Optional[str] = None
    new_hash: Optional[str] = None
    text: str = ''            # fetched main text (populated when we fetched it)
    title: str = ''           # page <title> when we fetched HTML (for documents.title)


# Locale-variant URLs pollute the brain with non-English rule text (e.g. the
# ?hl=zh-TW web.dev leak of 2026-07). Skip anything that is not the canonical
# English page BEFORE fetch/LLM spend.
_LOCALE_QUERY = re.compile(r'[?&]hl=(?!en(?:[-_]|$|&))', re.I)
_LOCALE_PATH = re.compile(
    r'//developer\.mozilla\.org/(?!en-US/)[a-z]{2}(?:-[A-Za-z]{2,4})?/'
    r'|//[^/]+/intl/[a-z]{2}'
    r'|//[^/]+/(?:zh-tw|zh-cn|zh-hans|zh-hant|ja|ko|de|es|fr|pt-br|ru|it|pl|tr|id|vi|th)/',
    re.I)


def _skip_locale(loc: str) -> bool:
    return bool(_LOCALE_QUERY.search(loc) or _LOCALE_PATH.search(loc))


def _norm_hash(text: str) -> str:
    return hashlib.sha256(re.sub(r'\s+', ' ', text or '').strip().encode()).hexdigest()


def _main_text(html: str) -> str:
    try:
        import trafilatura
        out = trafilatura.extract(html) or ''
        if out:
            return out
    except Exception:
        pass
    # stdlib fallback: strip tags
    t = re.sub(r'<script.*?</script>|<style.*?</style>', ' ', html, flags=re.S | re.I)
    return re.sub(r'<[^>]+>', ' ', t)


def _pdf_text(content: bytes) -> str:
    """Best-effort text from a PDF response (QRG etc.). Empty string on failure."""
    try:
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        parts = []
        for page in reader.pages[:40]:  # first 40 pages ≫ the 12k-char LLM window
            parts.append(page.extract_text() or '')
            if sum(len(p) for p in parts) > 60000:
                break
        return '\n'.join(parts)
    except Exception as e:
        log.warning('pdf extraction failed: %s', e)
        return ''


def _page_title(html: str) -> str:
    m = re.search(r'<title[^>]*>(.*?)</title>', html, re.S | re.I)
    if not m:
        return ''
    t = re.sub(r'\s+', ' ', m.group(1)).strip()
    return re.split(r'\s*[|–—-]\s{0,2}(?=[A-Z])', t)[0].strip()[:200] or t[:200]


def _response_text(r) -> tuple:
    """(main_text, title) for an HTTP response — HTML via trafilatura, PDF via pypdf."""
    ctype = (r.headers.get('content-type') or '').lower()
    if 'pdf' in ctype or r.url.path.lower().endswith('.pdf'):
        return _pdf_text(r.content), r.url.path.rsplit('/', 1)[-1]
    return _main_text(r.text), _page_title(r.text)


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------

def _detect_github_release(conn, source, client) -> List[ChangedURL]:
    """Schema.org etc.: latest release tag via the GitHub API."""
    m = re.search(r'github\.com/([^/]+)/([^/]+)', source.get('sitemap_url') or source['root_url'])
    if not m:
        return []
    api = f'https://api.github.com/repos/{m.group(1)}/{m.group(2)}/releases/latest'
    try:
        r = client.get(api, headers={**UA, 'Accept': 'application/vnd.github+json'})
        tag = r.json().get('tag_name') if r.status_code == 200 else None
    except Exception as e:
        log.warning('%s github check failed: %s', source['source_id'], e); return []
    if not tag:
        return []
    if tag == source.get('last_seen_marker'):
        return []  # no new release → nothing changed
    return [ChangedURL(url=source['root_url'], change_type='modified', signal='version',
                       old_hash=source.get('last_seen_marker'), new_hash=tag)]


def _detect_changelog(conn, source, client) -> List[ChangedURL]:
    """Google etc.: hash a single 'what's new' page; changed hash → re-ingest."""
    url = source['root_url']
    try:
        r = client.get(url, headers=UA)
        if r.status_code != 200:
            return []
        text = _main_text(r.text)
        h = _norm_hash(text)
    except Exception as e:
        log.warning('%s changelog check failed: %s', source['source_id'], e); return []
    prev = db.get_url_state(conn, url)
    if prev and prev.get('content_hash') == h:
        return []
    return [ChangedURL(url=url, change_type=('new' if not prev else 'modified'),
                       signal='content_hash', old_hash=(prev or {}).get('content_hash'),
                       new_hash=h, text=text)]


def _sitemap_urls(client, sitemap_url: str, limit: int = 300) -> List[tuple]:
    """Return [(loc, lastmod), ...] from a sitemap (follows one index level).
    Handles gzipped children (MDN-style .xml.gz)."""
    out = []
    try:
        r = client.get(sitemap_url, headers=UA)
        if r.status_code != 200:
            return []
        body = r.text
        # file-level gzip (e.g. developer.mozilla.org sitemap children)
        if sitemap_url.endswith('.gz') or r.content[:2] == b'\x1f\x8b':
            import gzip
            try:
                body = gzip.decompress(r.content).decode('utf-8', 'replace')
            except Exception:
                pass
        root = ET.fromstring(body)
        ns = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
        # sitemap index? Blog indexes (Yoast) list archives oldest-first, so
        # take the LAST children — that's where current posts live.
        children = [c.text for c in root.findall('.//sm:sitemap/sm:loc', ns) if c.text]
        if children:
            for child in children[-5:][::-1]:
                out.extend(_sitemap_urls(client, child, limit))
                if len(out) >= limit:
                    break
            return out[:limit]
        for u in root.findall('.//sm:url', ns):
            loc = u.find('sm:loc', ns)
            lm = u.find('sm:lastmod', ns)
            if loc is not None and loc.text and not _skip_locale(loc.text.strip()):
                out.append((loc.text.strip(), (lm.text.strip() if lm is not None and lm.text else None)))
    except Exception as e:
        log.warning('sitemap parse failed for %s: %s', sitemap_url, e)
    return out[:limit]


def _probe_url(conn, source, client, loc, lastmod=None):
    """Conditional-GET + content-hash a SINGLE page. Returns a ChangedURL if the
    page is new/changed (so its rules get THIS exact URL), else None. Shared by
    the sitemap and url_list adapters — this is what guarantees per-page (not
    hub) provenance: extraction stamps each rule with `loc`."""
    if _skip_locale(loc):
        return None
    prev = db.get_url_state(conn, loc)
    headers = dict(UA)
    if prev and prev.get('etag'):
        headers['If-None-Match'] = prev['etag']
    if prev and prev.get('last_modified'):
        headers['If-Modified-Since'] = prev['last_modified']
    try:
        r = client.get(loc, headers=headers)
    except Exception:
        return None
    if r.status_code == 304:  # server says unchanged
        db.save_url_state(conn, loc, source['source_id'], prev.get('etag'),
                          lastmod or (prev or {}).get('last_modified'), prev.get('content_hash'))
        return None
    if r.status_code != 200:
        return None
    text, title = _response_text(r)
    h = _norm_hash(text)
    etag = r.headers.get('ETag')
    if prev and prev.get('content_hash') == h:
        db.save_url_state(conn, loc, source['source_id'], etag, lastmod, h)
        return None
    return ChangedURL(url=loc, change_type=('new' if not prev else 'modified'),
                      signal='content_hash', old_hash=(prev or {}).get('content_hash'),
                      new_hash=h, text=text, title=title)


def _detect_sitemap(conn, source, client, max_fetch: int = 20,
                    max_probe: int = 80) -> List[ChangedURL]:
    """Two-stage: sitemap lastmod cheap-filter, then conditional-GET + content-hash.
    Each changed URL is a specific page, so rules extracted from it get its exact
    URL. `max_fetch` bounds CHANGED pages found (LLM spend); `max_probe` bounds
    total HTTP probes — without the distinction, sitemaps lacking <lastmod> would
    burn the whole budget re-probing known-unchanged pages and never advance."""
    sm = source.get('sitemap_url')
    if not sm:
        return []
    changed: List[ChangedURL] = []
    probed = 0
    for loc, lastmod in _sitemap_urls(client, sm):
        prev = db.get_url_state(conn, loc)
        # Stage 1 — lastmod cheap filter: if we've seen it and lastmod didn't move, skip.
        if prev and lastmod and prev.get('last_modified') == lastmod:
            continue
        if len(changed) >= max_fetch or probed >= max_probe:
            break
        probed += 1
        cu = _probe_url(conn, source, client, loc, lastmod)
        if cu:
            changed.append(cu)
    return changed


def _detect_url_list(conn, source, client, max_fetch: int = 60) -> List[ChangedURL]:
    """Crawl an EXPLICIT list of exact doc-page URLs (source.seed_urls). For
    sources with no clean SEO sitemap (Google Search Central, Perplexity, OpenAI,
    Bing), this captures each rule's PRECISE page URL instead of a generic hub."""
    urls = source.get('seed_urls') or []
    if isinstance(urls, str):
        import json
        try:
            urls = json.loads(urls)
        except Exception:
            urls = [u.strip() for u in urls.split(',') if u.strip()]
    changed: List[ChangedURL] = []
    for i, loc in enumerate(urls):
        if i >= max_fetch:
            break
        cu = _probe_url(conn, source, client, loc)
        if cu:
            changed.append(cu)
    return changed


_ADAPTERS = {
    'github_release': _detect_github_release,
    'changelog': _detect_changelog,
    'sitemap': _detect_sitemap,
    'url_list': _detect_url_list,
}


def detect(conn, source) -> List[ChangedURL]:
    """Dispatch to the source's adapter. Returns changed URLs (may be empty)."""
    adapter = _ADAPTERS.get(source.get('adapter_type', 'sitemap'), _detect_sitemap)
    # Generous timeout — some canonical sitemaps (web.dev, MDN) are large/slow.
    timeout = httpx.Timeout(float(os.getenv('INGEST_HTTP_TIMEOUT', '45')), connect=15.0)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        return adapter(conn, source, client)
