"""
Hacker News search via Algolia API (miner helper).

This is intended as a drop-in helper for AI search flows that need to populate
``ScraperStreamingSynapse.hacker_news_search_results`` with ``SearchResultItem``-shaped dicts.

API: https://hn.algolia.com/api
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal, Optional

import aiohttp

HN_ALGOLIA_BASE = "https://hn.algolia.com/api/v1"


SearchMode = Literal["search", "search_by_date"]
HNTag = Literal["story", "comment", "poll", "pollopt", "show_hn", "ask_hn", "front_page"]


@dataclass(frozen=True)
class HackerNewsQuery:
    query: str
    max_items: int = 10
    mode: SearchMode = "search"
    tags: Optional[list[str]] = None


def _hn_item_to_search_result(hit: dict[str, Any]) -> Optional[dict[str, str]]:
    title = (hit.get("title") or hit.get("story_title") or "").strip()
    url = (hit.get("url") or hit.get("story_url") or "").strip()
    if not url:
        object_id = hit.get("objectID")
        if object_id:
            url = f"https://news.ycombinator.com/item?id={object_id}"
    if not title or not url:
        return None

    snippet = (hit.get("story_text") or hit.get("comment_text") or hit.get("_highlightResult") or "")
    if isinstance(snippet, dict):
        snippet = ""
    if not isinstance(snippet, str):
        snippet = ""

    return {"title": title, "link": url, "snippet": snippet}


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

