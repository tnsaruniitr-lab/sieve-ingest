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
    """Return [(loc, lastmod), ...] from a sitemap (follows one index level)."""
    out = []
    try:
        r = client.get(sitemap_url, headers=UA)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.text)
        ns = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
        # sitemap index?
        children = [c.text for c in root.findall('.//sm:sitemap/sm:loc', ns) if c.text]
        if children:
            for child in children[:5]:
                out.extend(_sitemap_urls(client, child, limit))
                if len(out) >= limit:
                    break
            return out[:limit]
        for u in root.findall('.//sm:url', ns):
            loc = u.find('sm:loc', ns)
            lm = u.find('sm:lastmod', ns)
            if loc is not None and loc.text:
                out.append((loc.text.strip(), (lm.text.strip() if lm is not None and lm.text else None)))
    except Exception as e:
        log.warning('sitemap parse failed for %s: %s', sitemap_url, e)
    return out[:limit]


def _detect_sitemap(conn, source, client, max_fetch: int = 20) -> List[ChangedURL]:
    """Two-stage: sitemap lastmod cheap-filter, then conditional-GET + content-hash."""
    sm = source.get('sitemap_url')
    if not sm:
        return []
    changed: List[ChangedURL] = []
    fetched = 0
    for loc, lastmod in _sitemap_urls(client, sm):
        prev = db.get_url_state(conn, loc)
        # Stage 1 — lastmod cheap filter: if we've seen it and lastmod didn't move, skip.
        if prev and lastmod and prev.get('last_modified') == lastmod:
            continue
        if fetched >= max_fetch:
            break
        # Stage 2 — conditional GET + content hash.
        headers = dict(UA)
        if prev and prev.get('etag'):
            headers['If-None-Match'] = prev['etag']
        if prev and prev.get('last_modified'):
            headers['If-Modified-Since'] = prev['last_modified']
        try:
            r = client.get(loc, headers=headers)
        except Exception:
            continue
        fetched += 1
        if r.status_code == 304:  # server says unchanged
            db.save_url_state(conn, loc, source['source_id'], prev.get('etag'), lastmod,
                              prev.get('content_hash'))
            continue
        if r.status_code != 200:
            continue
        text = _main_text(r.text)
        h = _norm_hash(text)
        etag = r.headers.get('ETag')
        if prev and prev.get('content_hash') == h:
            db.save_url_state(conn, loc, source['source_id'], etag, lastmod, h)
            continue
        changed.append(ChangedURL(url=loc, change_type=('new' if not prev else 'modified'),
                                   signal='content_hash', old_hash=(prev or {}).get('content_hash'),
                                   new_hash=h, text=text))
    return changed


_ADAPTERS = {
    'github_release': _detect_github_release,
    'changelog': _detect_changelog,
    'sitemap': _detect_sitemap,
}


def detect(conn, source) -> List[ChangedURL]:
    """Dispatch to the source's adapter. Returns changed URLs (may be empty)."""
    adapter = _ADAPTERS.get(source.get('adapter_type', 'sitemap'), _detect_sitemap)
    # Generous timeout — some canonical sitemaps (web.dev, MDN) are large/slow.
    timeout = httpx.Timeout(float(os.getenv('INGEST_HTTP_TIMEOUT', '45')), connect=15.0)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        return adapter(conn, source, client)
