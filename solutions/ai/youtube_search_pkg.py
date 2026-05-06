"""
YouTube search via PyPI ``youtube-search`` (scrapes YouTube results HTML).

Populates ``ScraperStreamingSynapse.youtube_search_results`` with
``SearchResultItem``-shaped dicts: ``{title, link, snippet}``.

Package: https://pypi.org/project/youtube-search/
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Optional

from youtube_search import YoutubeSearch


@dataclass(frozen=True)
class YoutubeQuery:
    query: str
    max_items: int = 10


def _video_to_item(v: dict) -> Optional[dict[str, str]]:
    title = (v.get("title") or "").strip()
    vid = v.get("id")
    suffix = v.get("url_suffix") or ""
    link = ""
    if isinstance(suffix, str) and suffix.startswith("http"):
        link = suffix.strip()
    elif isinstance(suffix, str) and suffix.startswith("/"):
        link = f"https://www.youtube.com{suffix}"
    elif vid:
        link = f"https://www.youtube.com/watch?v={vid}"
    if not title or not link:
        return None

    parts: list[str] = []
    if v.get("long_desc"):
        parts.append(str(v["long_desc"]).strip())
    if v.get("channel"):
        parts.append(f"Channel: {v['channel']}")
    if v.get("views"):
        parts.append(str(v["views"]))
    if v.get("duration"):
        parts.append(str(v["duration"]))
    snippet = " · ".join(p for p in parts if p)
    return {"title": title, "link": link, "snippet": snippet}


def _run_youtube_search_sync(q: YoutubeQuery) -> list[dict[str, str]]:
    text = (q.query or "").strip()
    if not text:
        return []
    n = max(1, min(int(q.max_items or 10), 50))
    raw = YoutubeSearch(text, max_results=n).to_dict()
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for v in raw:
        if not isinstance(v, dict):
            continue
        item = _video_to_item(v)
        if item:
            out.append(item)
        if len(out) >= n:
            break
    return out


async def youtube_search(q: YoutubeQuery) -> list[dict[str, str]]:
    return await asyncio.to_thread(_run_youtube_search_sync, q)


async def fill_youtube_results(
    synapse: Any,
    *,
    query: Optional[str] = None,
    max_items: Optional[int] = None,
) -> Any:
    q = query if query is not None else getattr(synapse, "prompt", "") or ""
    n = max_items if max_items is not None else getattr(synapse, "max_items", None)
    n = int(n or 10)
    rows = await youtube_search(YoutubeQuery(query=str(q), max_items=n))
    setattr(synapse, "youtube_search_results", rows)
    return synapse


def run_youtube_search_sync(query: str, *, max_items: int = 10) -> list[dict[str, str]]:
    return _run_youtube_search_sync(YoutubeQuery(query=query, max_items=max_items))
