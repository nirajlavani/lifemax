"""Curated AI news feed (RSS) with X-link badging and Open Graph fallback.

We aggregate a few well-known AI feeds, score each item by AI-keyword presence,
and surface the most recent ~18. Items whose link points to x.com / twitter.com
get an `is_x: true` flag so the UI can badge them.

Hardening notes (BRAND · 11):
- HN entries with score < 3 AND no inline image are dropped — that was the
  primary cause of the slideshow looking dead. The HN feed has no `media:*`
  payload, so without OG fallback we'd just show three boring titles.
- For items with no inline image, we attempt a single bounded HEAD/GET against
  the article URL to extract `<meta property="og:image">`. The fetch is
  capped at 256 KB / 4s, runs at most once per article URL per process, and
  validates the resulting URL just like the inline path does.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import socket
import time
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any
from urllib.parse import urljoin, urlparse

import feedparser  # type: ignore[import-untyped]
import httpx

from ..config import get_settings

logger = logging.getLogger(__name__)

_NEWS_TTL_SECONDS = 15 * 60  # 15 minutes
_MAX_ITEMS = 18
_DESC_MAX_CHARS = 320

# Open Graph fallback budget — keep it tight so a slow newsroom can never
# stall the dashboard. We only fetch when an item has no inline image.
_OG_FETCH_TIMEOUT_S = 4.0
_OG_FETCH_BODY_CAP_BYTES = 256 * 1024  # 256 KB head is plenty for <head>
_OG_PARALLELISM = 6                    # simultaneous fetches per refresh
_OG_HEAD_REGION_CHARS = 32 * 1024      # only scan the first 32 KB for OG tags
_OG_USER_AGENT = "Mozilla/5.0 (compatible; LifemaxDashboard/0.1; +bot)"

# Hacker News quality filter — ditch low-effort, no-image submissions.
_HN_HOST_RE = re.compile(r"(?:^|\.)news\.ycombinator\.com$", re.IGNORECASE)
_HN_MIN_SCORE_WITHOUT_IMAGE = 3

# Strip HTML tags for description preview. RSS summaries are author-provided
# and considered untrusted input; we never render them as HTML on the client.
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
# Pull <img src="..."> out of summary/content for an inline thumbnail when no
# media:content / enclosure is present.
_IMG_SRC_RE = re.compile(
    r"""<img[^>]+src\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
# OG image meta inside <head>. Tolerates either property/content order.
_OG_IMAGE_RE = re.compile(
    r"""<meta\s+[^>]*?(?:property|name)\s*=\s*['"]og:image(?::secure_url)?['"][^>]*?content\s*=\s*['"]([^'"]+)['"]""",
    re.IGNORECASE | re.DOTALL,
)
_OG_IMAGE_REVERSE_RE = re.compile(
    r"""<meta\s+[^>]*?content\s*=\s*['"]([^'"]+)['"][^>]*?(?:property|name)\s*=\s*['"]og:image(?::secure_url)?['"]""",
    re.IGNORECASE | re.DOTALL,
)
_TWITTER_IMAGE_RE = re.compile(
    r"""<meta\s+[^>]*?name\s*=\s*['"]twitter:image['"][^>]*?content\s*=\s*['"]([^'"]+)['"]""",
    re.IGNORECASE | re.DOTALL,
)
_IMAGE_EXT_RE = re.compile(r"\.(?:jpg|jpeg|png|gif|webp|avif)(?:\?|$)", re.IGNORECASE)

_AI_KEYWORDS = (
    "ai",
    "artificial intelligence",
    "llm",
    "agent",
    "agents",
    "openai",
    "anthropic",
    "claude",
    "gemini",
    "deepseek",
    "mistral",
    "transformer",
    "rag",
    "fine-tune",
    "fine tune",
    "model",
    "machine learning",
    "ml ",
    "neural",
    "embedding",
    "diffusion",
    "inference",
)

_X_HOSTS = {"x.com", "twitter.com", "www.x.com", "www.twitter.com"}


@dataclass(slots=True)
class _NewsCache:
    items: list[dict[str, Any]]
    fetched_at: float


def _is_x_link(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in _X_HOSTS


def _ai_score(text: str) -> int:
    needle = text.lower()
    return sum(1 for kw in _AI_KEYWORDS if kw in needle)


def _clean_description(html_text: str, *, limit: int = _DESC_MAX_CHARS) -> str:
    """Strip HTML and collapse whitespace into a short plain-text preview.

    RSS summaries are untrusted author input; we deliberately discard markup
    rather than sanitize-and-render to avoid XSS exposure on the dashboard.
    """
    if not html_text:
        return ""
    text = _TAG_RE.sub(" ", html_text)
    text = html.unescape(text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return f"{cut}\u2026"


def _safe_image_url(candidate: str | None, base_url: str) -> str | None:
    """Validate and resolve an image URL, rejecting non-http(s) schemes."""
    if not candidate:
        return None
    candidate = candidate.strip()
    if not candidate:
        return None
    try:
        resolved = urljoin(base_url, candidate)
        parsed = urlparse(resolved)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    if not parsed.netloc:
        return None
    return resolved


def _extract_image(entry: Any, link: str) -> str | None:
    """Pull a representative image URL from an RSS entry, if any.

    Order of preference: media:content > media:thumbnail > enclosure (image/*)
    > <img> tag inside summary/content. Returns None when nothing is suitable.
    """
    candidates: list[str] = []

    media_content = entry.get("media_content") if isinstance(entry, dict) else getattr(entry, "media_content", None)
    if media_content:
        for m in media_content:
            url = m.get("url") if isinstance(m, dict) else None
            if url:
                candidates.append(url)

    media_thumbnail = entry.get("media_thumbnail") if isinstance(entry, dict) else getattr(entry, "media_thumbnail", None)
    if media_thumbnail:
        for m in media_thumbnail:
            url = m.get("url") if isinstance(m, dict) else None
            if url:
                candidates.append(url)

    enclosures = entry.get("enclosures") if isinstance(entry, dict) else getattr(entry, "enclosures", None)
    if enclosures:
        for enc in enclosures:
            etype = (enc.get("type") or "") if isinstance(enc, dict) else ""
            url = enc.get("href") or enc.get("url") if isinstance(enc, dict) else None
            if url and (etype.startswith("image/") or _IMAGE_EXT_RE.search(url)):
                candidates.append(url)

    summary = entry.get("summary") or ""
    content_list = entry.get("content") or []
    bodies = [summary]
    if isinstance(content_list, list):
        for c in content_list:
            value = c.get("value") if isinstance(c, dict) else None
            if value:
                bodies.append(value)
    for body in bodies:
        if not body:
            continue
        m = _IMG_SRC_RE.search(body)
        if m:
            candidates.append(m.group(1))
            break

    for c in candidates:
        safe = _safe_image_url(c, link)
        if safe:
            return safe
    return None


def _entry_published_ts(entry: Any) -> float:
    for key in ("published_parsed", "updated_parsed"):
        struct = getattr(entry, key, None) or entry.get(key) if isinstance(entry, dict) else None
        if struct:
            try:
                return time.mktime(struct)
            except (OverflowError, ValueError, TypeError):
                continue
    return 0.0


def _hn_score_from_summary(summary: str) -> int | None:
    """Best-effort points extraction from an `hnrss.org` description.

    The hnrss summary is plain text like
    ``Article URL: ...\nComments URL: ...\nPoints: 42\nComments: 7``.
    We only need a rough integer to gate the no-image filter.
    """
    if not summary:
        return None
    m = re.search(r"Points:\s*(\d+)", summary, re.IGNORECASE)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _parse_feed_sync(url: str) -> list[dict[str, Any]]:
    """Blocking feedparser call. Run via `asyncio.to_thread`."""
    parsed = feedparser.parse(url)
    feed_title = parsed.feed.get("title", "") if parsed.feed else ""
    items: list[dict[str, Any]] = []
    for entry in parsed.entries[:30]:
        link = (entry.get("link") or "").strip()
        title = (entry.get("title") or "").strip()
        if not link or not title:
            continue
        summary = (entry.get("summary") or "").strip()
        published_ts = _entry_published_ts(entry)
        score = _ai_score(f"{title} {summary}")
        if score == 0:
            # Skip non-AI items even from broad feeds.
            continue
        description = _clean_description(summary)
        image = _extract_image(entry, link)

        # Hacker News quality gate — drop low-effort, no-image submissions.
        # We do this in the parser (not the aggregator) so the cap-of-30
        # window is spent on real candidates rather than HN noise.
        try:
            link_host = (urlparse(link).hostname or "").lower()
        except ValueError:
            link_host = ""
        is_hn = "hnrss.org" in url or "ycombinator.com" in (feed_title or "").lower() or _HN_HOST_RE.search(link_host or "")
        if is_hn and image is None:
            hn_points = _hn_score_from_summary(summary)
            if hn_points is None or hn_points < _HN_MIN_SCORE_WITHOUT_IMAGE:
                continue

        items.append(
            {
                "title": title,
                "link": link,
                "source": feed_title,
                "description": description,
                "image": image,
                "published_ts": published_ts,
                "score": score,
                "is_x": _is_x_link(link),
            }
        )
    return items


# ---------------------------------------------------------------------------
# Open Graph image fallback
# ---------------------------------------------------------------------------

def _og_url_is_safe_external(url: str) -> bool:
    """Block obvious SSRF targets for the OG fetch path.

    The dashboard runs on a personal Mac mini so the blast radius is small,
    but we still refuse to issue requests at private/loopback/link-local
    addresses just in case a feed advertises one.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = parsed.hostname
    if not host:
        return False
    # Numeric host? Reject anything not in the public unicast space.
    try:
        addr = ip_address(host)
        return not (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
        )
    except ValueError:
        pass
    # Hostname — refuse obviously local-only suffixes.
    lowered = host.lower()
    if lowered in {"localhost"} or lowered.endswith(".local") or lowered.endswith(".internal"):
        return False
    # Best-effort DNS check; if resolution fails, allow httpx to try and just
    # error out. We don't want an outage at our resolver to kill all news.
    try:
        for info in socket.getaddrinfo(host, None):
            try:
                addr = ip_address(info[4][0])
            except (ValueError, IndexError):
                continue
            if addr.is_private or addr.is_loopback or addr.is_link_local:
                return False
    except socket.gaierror:
        return True
    return True


def _scan_og_image(html_text: str, base_url: str) -> str | None:
    """Find an OG image inside an HTML head snippet."""
    if not html_text:
        return None
    head = html_text[:_OG_HEAD_REGION_CHARS]
    for pattern in (_OG_IMAGE_RE, _OG_IMAGE_REVERSE_RE, _TWITTER_IMAGE_RE):
        m = pattern.search(head)
        if m:
            safe = _safe_image_url(html.unescape(m.group(1)), base_url)
            if safe:
                return safe
    return None


async def _fetch_og_image(client: httpx.AsyncClient, link: str) -> str | None:
    """Fetch up to ~256 KB of an article and look for an OG image."""
    if not _og_url_is_safe_external(link):
        return None
    try:
        async with client.stream(
            "GET",
            link,
            timeout=_OG_FETCH_TIMEOUT_S,
            follow_redirects=True,
            headers={
                "User-Agent": _OG_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.7",
            },
        ) as resp:
            if resp.status_code >= 400:
                return None
            ctype = (resp.headers.get("content-type") or "").lower()
            if ctype and "html" not in ctype and "xml" not in ctype:
                return None
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf.extend(chunk)
                if len(buf) >= _OG_FETCH_BODY_CAP_BYTES:
                    break
        text = buf[:_OG_FETCH_BODY_CAP_BYTES].decode("utf-8", errors="ignore")
        return _scan_og_image(text, str(resp.url))
    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
        logger.debug("og image fetch failed (%s): %s", link, exc)
        return None


async def _hydrate_og_images(items: list[dict[str, Any]]) -> None:
    """Mutate items in place, filling `image` with OG fallbacks where missing."""
    targets = [i for i in items if not i.get("image") and i.get("link")]
    if not targets:
        return
    sem = asyncio.Semaphore(_OG_PARALLELISM)
    timeout = httpx.Timeout(_OG_FETCH_TIMEOUT_S, connect=_OG_FETCH_TIMEOUT_S)
    limits = httpx.Limits(max_connections=_OG_PARALLELISM, max_keepalive_connections=_OG_PARALLELISM)
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        async def _one(item: dict[str, Any]) -> None:
            async with sem:
                image = await _fetch_og_image(client, item["link"])
                if image:
                    item["image"] = image

        await asyncio.gather(*[_one(i) for i in targets], return_exceptions=False)


class NewsWidget:
    """Async news aggregator with TTL cache."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._cache: _NewsCache | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> list[dict[str, Any]]:
        async with self._lock:
            now = time.time()
            if (
                self._cache is not None
                and (now - self._cache.fetched_at) < _NEWS_TTL_SECONDS
            ):
                return self._cache.items
            collected: list[dict[str, Any]] = []
            for url in self._settings.news_feeds:
                try:
                    items = await asyncio.to_thread(_parse_feed_sync, url)
                    collected.extend(items)
                except Exception as exc:  # noqa: BLE001 - one bad feed should not break the rest
                    logger.warning("news feed failed (%s): %s", url, exc)
                    continue
            collected.sort(key=lambda i: (i["published_ts"], i["score"]), reverse=True)
            trimmed = collected[:_MAX_ITEMS]
            try:
                await _hydrate_og_images(trimmed)
            except Exception as exc:  # noqa: BLE001 - hydration should never break the cache
                logger.warning("og image hydration failed: %s", exc)
            self._cache = _NewsCache(items=trimmed, fetched_at=now)
            return trimmed
