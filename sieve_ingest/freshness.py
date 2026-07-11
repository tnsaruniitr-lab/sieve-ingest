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
    change_type: str          # new | modified | removed
    signal: str               # lastmod | etag | content_hash | version | gone
    old_hash: Optional[str] = None
    new_hash: Optional[str] = None
    text: str = ''            # fetched main text (populated when we fetched it)
    etag: Optional[str] = None       # response ETag — persisted so conditional GETs work next cycle
    lastmod: Optional[str] = None    # sitemap lastmod — persisted for the stage-1 cheap filter


def _norm_hash(text: str) -> str:
    return hashlib.sha256(re.sub(r'\s+', ' ', text or '').strip().encode()).hexdigest()


# Tracking/duplicate query params that fan one page out into many URLs
# (the Jul-6 run burned web.dev's whole budget on one article ×5 ?hl= locales).
# Deliberately NOT 'ref'/'source' — those can be semantic on doc sites.
_JUNK_PARAMS = re.compile(r'^(hl|utm_[a-z]+|gclid|fbclid)$', re.I)

# Hard denylist — never worth a fetch or an LLM call. Ported from sieve-crawler's
# proven filter checklist + the exact page types the Jul-6 run ingested (404,
# /about, /advertising, site chrome).
#   - Chrome words are anchored to the path ROOT (at most one locale/prefix
#     segment before them): /about and /en-US/about are chrome; deep doc paths
#     like /en-US/docs/Web/Privacy or /docs/authentication are CONTENT.
#   - 'search' is deliberately NOT in the list — developers.google.com/search/*
#     is core content; query-based search pages are caught by _DENY_QUERY.
_DENY_ROOT = re.compile(
    r'^(?:/[^/]+)?/(?:login|signin|signup|register|account|dashboard|auth'
    r'|404|about|advertising|careers|jobs|privacy|terms|legal|contact'
    r'|newsletter)(?:[/?#]|$)', re.I)
_DENY_PATH = re.compile(
    r'\.(?:pdf|jpg|jpeg|png|gif|svg|webp|mp4|mp3|zip|tar|gz|css|js)$'
    r'|/page/\d|/tags?/|/tags?$|/category/|/feed/?$|/rss/?$|\.atom$', re.I)
_DENY_QUERY = re.compile(r'(?:^|&)(?:q|search)=|(?:^|&)page=\d', re.I)


def normalize_url(url: str) -> str:
    """Canonical form for fingerprinting: strip fragment + junk params, collapse
    the trailing slash. One page = one url_state row, whatever locale/utm noise
    the sitemap decorates it with."""
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    parts = urlsplit(url.strip())
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
         if not _JUNK_PARAMS.match(k)]
    path = parts.path.rstrip('/') or '/'
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(q), ''))


def url_allowed(source, url: str) -> bool:
    """Global denylist + optional per-source allow-regex (matched against the
    URL path). Applied BEFORE any fetch/LLM spend."""
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    if (_DENY_ROOT.search(parts.path) or _DENY_PATH.search(parts.path)
            or _DENY_QUERY.search(parts.query)):
        return False
    allow = source.get('url_filter')
    if allow:
        try:
            if not re.search(allow, parts.path):
                return False
        except re.error:
            log.warning('%s has invalid url_filter %r — ignoring it',
                        source.get('source_id'), allow)
    return True


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
    """Schema.org etc.: latest release tag via the GitHub API. The release NOTES
    body rides along as the extraction text — without it every release would be
    consumed as 'empty' and the brain would never learn what changed."""
    m = re.search(r'github\.com/([^/]+)/([^/]+)', source.get('sitemap_url') or source['root_url'])
    if not m:
        return []
    api = f'https://api.github.com/repos/{m.group(1)}/{m.group(2)}/releases/latest'
    try:
        r = client.get(api, headers={**UA, 'Accept': 'application/vnd.github+json'})
        rel = r.json() if r.status_code == 200 else {}
        tag = rel.get('tag_name')
    except Exception as e:
        log.warning('%s github check failed: %s', source['source_id'], e); return []
    if not tag:
        return []
    if tag == source.get('last_seen_marker'):
        return []  # no new release → nothing changed
    notes = f"{rel.get('name') or tag}\n\n{rel.get('body') or ''}".strip()
    url = rel.get('html_url') or source['root_url']  # exact release page, not the hub
    return [ChangedURL(url=url, change_type='modified', signal='version',
                       old_hash=source.get('last_seen_marker'), new_hash=tag,
                       text=notes)]


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


def _sitemap_children(client, sitemap_url: str) -> Optional[List[str]]:
    """If the sitemap is an INDEX, return its child sitemap URLs; None if it is
    a direct urlset (or unreadable)."""
    try:
        r = client.get(sitemap_url, headers=UA)
        if r.status_code != 200:
            return None
        root = ET.fromstring(r.text)
        ns = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
        children = [c.text for c in root.findall('.//sm:sitemap/sm:loc', ns) if c.text]
        return children or None
    except Exception as e:
        log.warning('sitemap index parse failed for %s: %s', sitemap_url, e)
        return None


def _urlset_urls(client, sitemap_url: str, limit: int = 300) -> List[tuple]:
    """Return [(loc, lastmod), ...] from a single (non-index) sitemap."""
    out = []
    try:
        r = client.get(sitemap_url, headers=UA)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.text)
        ns = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
        for u in root.findall('.//sm:url', ns):
            loc = u.find('sm:loc', ns)
            lm = u.find('sm:lastmod', ns)
            if loc is not None and loc.text:
                out.append((loc.text.strip(),
                            (lm.text.strip() if lm is not None and lm.text else None)))
    except Exception as e:
        log.warning('sitemap parse failed for %s: %s', sitemap_url, e)
    return out[:limit]


def _sitemap_urls(client, sitemap_url: str, limit: int = 300,
                  start_child: int = 0) -> List[tuple]:
    """[(loc, lastmod), ...] from a sitemap, following one index level starting
    at child `start_child` (the rotation cursor's window). Kept for callers
    that don't rotate (probe-source, url-enrichment)."""
    children = _sitemap_children(client, sitemap_url)
    if children is None:
        return _urlset_urls(client, sitemap_url, limit)
    out: List[tuple] = []
    n = len(children)
    for i in range(n):
        child = children[(start_child + i) % n]
        out.extend(_urlset_urls(client, child, limit))
        if len(out) >= limit:
            break
    return out[:limit]


def _probe_url(conn, source, client, loc, lastmod=None):
    """Conditional-GET + content-hash a SINGLE page. Returns a ChangedURL if the
    page is new/changed (so its rules get THIS exact URL), else None. Shared by
    the sitemap and url_list adapters — this is what guarantees per-page (not
    hub) provenance: extraction stamps each rule with `loc`."""
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
    if r.status_code in (404, 410) and prev:
        # A page we HAD fingerprinted is gone — the retire signal. Rules that
        # cite it must stop being served as current guidance.
        return ChangedURL(url=loc, change_type='removed', signal='gone',
                          old_hash=prev.get('content_hash'), new_hash=None)
    if r.status_code != 200:
        return None
    text = _main_text(r.text)
    h = _norm_hash(text)
    etag = r.headers.get('ETag')
    if prev and prev.get('content_hash') == h:
        db.save_url_state(conn, loc, source['source_id'], etag, lastmod, h)
        return None
    return ChangedURL(url=loc, change_type=('new' if not prev else 'modified'),
                      signal='content_hash', old_hash=(prev or {}).get('content_hash'),
                      new_hash=h, text=text, etag=etag, lastmod=lastmod)


def _detect_sitemap(conn, source, client, max_fetch: int = 20) -> List[ChangedURL]:
    """Three-stage per cycle, so full coverage accumulates across cycles even on
    14k-URL sites without ever re-paying for what's known:

      0. RETRY FIRST: URLs with unconsumed failed changes are re-probed before
         anything else — the rotation cursor must never strand a failed page.
      1. Window walk: children of a sitemap index are processed starting at the
         source's crawl_cursor; the cursor advances (and wraps) each cycle.
      2. Per URL: hygiene filters → lastmod cheap-filter → conditional-GET +
         content-hash (the definitive signal).

    Each changed URL is a specific page, so its rules get its exact URL."""
    sm = source.get('sitemap_url')
    if not sm:
        return []
    changed: List[ChangedURL] = []
    fetched = 0
    seen: set = set()

    # Stage 0 — retry-first: failed-and-unconsumed changes from prior cycles.
    for loc in db.pending_retry_urls(conn, source['source_id'], limit=max_fetch):
        loc = normalize_url(loc)
        if loc in seen:
            continue
        seen.add(loc)
        fetched += 1
        cu = _probe_url(conn, source, client, loc)
        if cu:
            changed.append(cu)

    # Stage 1+2 — cursor-rotated window over the sitemap.
    children = _sitemap_children(client, sm)
    cursor = db.get_crawl_cursor(conn, source['source_id'])
    if children is None:
        url_iter = _urlset_urls(client, sm)
        # Direct urlset: rotate by offset so later pages get their turn.
        off = cursor.get('offset', 0) % max(len(url_iter), 1)
        url_iter = url_iter[off:] + url_iter[:off]
        next_cursor = {'offset': (off + max_fetch) % max(len(url_iter), 1)}
    else:
        n = len(children)
        start = cursor.get('child', 0) % n
        url_iter = []
        for i in range(n):
            url_iter.extend(_urlset_urls(client, children[(start + i) % n]))
            if len(url_iter) >= 300:
                break
        next_cursor = {'child': (start + 1) % n}

    for raw_loc, lastmod in url_iter:
        loc = normalize_url(raw_loc)
        # Hygiene BEFORE any fetch: one page = one probe (?hl=/utm dupes collapse),
        # and denylisted/out-of-scope pages never cost a request or an LLM call.
        if loc in seen or not url_allowed(source, loc):
            continue
        seen.add(loc)
        prev = db.get_url_state(conn, loc)
        # lastmod cheap filter: if we've seen it and lastmod didn't move, skip.
        if prev and lastmod and prev.get('last_modified') == lastmod:
            continue
        if fetched >= max_fetch:
            break
        fetched += 1
        cu = _probe_url(conn, source, client, loc, lastmod)
        if cu:
            changed.append(cu)
    db.save_crawl_cursor(conn, source['source_id'], next_cursor)
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
    seen: set = set()
    for loc in urls:
        if len(seen) >= max_fetch:
            break
        loc = normalize_url(loc)
        if loc in seen:  # seed_urls are curated — denylist does not apply here
            continue
        seen.add(loc)
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
