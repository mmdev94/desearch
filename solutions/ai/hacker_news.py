"""
Hacker News search via Algolia API (miner helper).

This is intended as a drop-in helper for AI search flows that need to populate
``ScraperStreamingSynapse.hacker_news_search_results`` with ``SearchResultItem``-shaped dicts.

API: https://hn.algolia.com/api

**Links** are always ``https://news.ycombinator.com/item?id=…`` so validators treat them
as ``ycombinator.com`` and attribute them to ``hacker_news_search_results`` (see
``search_content_relevance.check_response_random_link``). External story URLs from
Algolia are not used.

**Snippets** are plain text from ``story_text`` / ``comment_text`` or stripped
``_highlightResult`` fragments—no raw highlight dict blobs.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from html import unescape
from typing import Any, Literal, Optional

import aiohttp

HN_ALGOLIA_BASE = "https://hn.algolia.com/api/v1"
HN_ITEM_BASE = "https://news.ycombinator.com/item"

_HTML_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)
_EMP_TAG_RE = re.compile(r"</?em>", re.IGNORECASE)


SearchMode = Literal["search", "search_by_date"]
HNTag = Literal["story", "comment", "poll", "pollopt", "show_hn", "ask_hn", "front_page"]


@dataclass(frozen=True)
class HackerNewsQuery:
    query: str
    max_items: int = 10
    mode: SearchMode = "search"
    tags: Optional[list[str]] = None


def _strip_html_to_text(raw: str) -> str:
    if not raw or not isinstance(raw, str):
        return ""
    t = _HTML_TAG_RE.sub(" ", raw)
    t = _EMP_TAG_RE.sub("", t)
    t = unescape(t)
    return " ".join(t.split()).strip()


def _highlight_snippet_plain(hit: dict[str, Any]) -> str:
    hr = hit.get("_highlightResult")
    if not isinstance(hr, dict):
        return ""
    parts: list[str] = []
    for key in ("story_text", "comment_text", "title"):
        blk = hr.get(key)
        if isinstance(blk, dict):
            val = blk.get("value")
            if isinstance(val, str) and val.strip():
                parts.append(_strip_html_to_text(val))
    return " ".join(parts).strip()


def _snippet_from_hit(hit: dict[str, Any]) -> str:
    for key in ("story_text", "comment_text"):
        raw = hit.get(key)
        if isinstance(raw, str) and raw.strip():
            sn = _strip_html_to_text(raw.strip())
            if sn:
                return sn[:1500]
    hl = _highlight_snippet_plain(hit)
    if hl:
        return hl[:1500]
    title = (hit.get("title") or hit.get("story_title") or "").strip()
    if title:
        return title[:800]
    return "Hacker News"


def _canonical_hn_item_link(hit: dict[str, Any]) -> Optional[str]:
    """Always news.ycombinator.com so domain attribution matches ``hacker_news_search_results``."""
    oid = hit.get("objectID")
    if oid is None or str(oid).strip() == "":
        return None
    return f"{HN_ITEM_BASE}?id={str(oid).strip()}"


def _hn_item_to_search_result(hit: dict[str, Any]) -> Optional[dict[str, str]]:
    link = _canonical_hn_item_link(hit)
    if not link:
        return None

    title = (hit.get("title") or hit.get("story_title") or "").strip()
    if not title:
        title = f"Hacker News item {hit.get('objectID', '')}".strip()

    snippet = _snippet_from_hit(hit)

    return {"title": title, "link": link, "snippet": snippet}


async def hn_algolia_search(q: HackerNewsQuery) -> list[dict[str, str]]:
    """
    Return a list of ``SearchResultItem`` dicts: {title, link, snippet}.
    """
    query = (q.query or "").strip()
    if not query:
        return []

    max_items = max(1, min(int(q.max_items or 10), 100))
    mode: SearchMode = q.mode if q.mode in ("search", "search_by_date") else "search"

    endpoint = f"{HN_ALGOLIA_BASE}/{mode}"

    params: dict[str, Any] = {
        "query": query,
        "hitsPerPage": str(max_items),
    }
    if q.tags:
        # Algolia expects tags as comma-separated values (e.g. "story" or "(story,ask_hn)").
        params["tags"] = ",".join(str(t).strip() for t in q.tags if str(t).strip())

    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(endpoint, params=params) as resp:
            if resp.status != 200:
                return []
            payload = await resp.json()

    hits = payload.get("hits") or []
    out: list[dict[str, str]] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        item = _hn_item_to_search_result(hit)
        if item:
            out.append(item)
        if len(out) >= max_items:
            break
    return out


async def fill_hacker_news_results(
    synapse: Any,
    *,
    query: Optional[str] = None,
    max_items: Optional[int] = None,
    mode: SearchMode = "search",
    tags: Optional[list[str]] = None,
) -> Any:
    """
    Populate ``synapse.hacker_news_search_results`` in-place and return the synapse.

    - Reads ``synapse.prompt`` as the default query if ``query`` is None.
    - Reads ``synapse.max_items`` as default max_items if ``max_items`` is None.
    """
    q = query if query is not None else getattr(synapse, "prompt", "") or ""
    n = max_items if max_items is not None else getattr(synapse, "max_items", None)
    n = int(n or 10)

    results = await hn_algolia_search(HackerNewsQuery(query=str(q), max_items=n, mode=mode, tags=tags))

    # Allow both list[SearchResultItem] models and raw list[dict] consumers.
    setattr(synapse, "hacker_news_search_results", results)
    return synapse


def run_hn_algolia_search_sync(
    query: str,
    *,
    max_items: int = 10,
    mode: SearchMode = "search",
    tags: Optional[list[str]] = None,
) -> list[dict[str, str]]:
    """
    Convenience sync wrapper for quick manual testing.
    """
    return asyncio.run(
        hn_algolia_search(HackerNewsQuery(query=query, max_items=max_items, mode=mode, tags=tags))
    )

