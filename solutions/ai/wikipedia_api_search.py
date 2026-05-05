"""
Wikipedia search via ``wikipedia-api`` (martin-majlis/Wikipedia-API).

Populates ``ScraperStreamingSynapse.wikipedia_search_results`` with
``SearchResultItem``-shaped dicts: ``{title, link, snippet}``.

Wikimedia requires a descriptive User-Agent:
https://meta.wikimedia.org/wiki/User-Agent_policy

Set ``WIKIPEDIA_USER_AGENT`` in the environment (recommended), or a default is used.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote

import wikipediaapi


_DEFAULT_USER_AGENT = (
    "DesearchSolutions/1.0 (https://github.com/Desearch-ai/subnet-22; miner helper)"
)


@dataclass(frozen=True)
class WikipediaQuery:
    query: str
    max_items: int = 10
    language: str = "en"


def _user_agent() -> str:
    return (os.environ.get("WIKIPEDIA_USER_AGENT") or _DEFAULT_USER_AGENT).strip()


def _wiki_client(language: str) -> wikipediaapi.Wikipedia:
    lang = (language or "en").strip() or "en"
    return wikipediaapi.Wikipedia(
        user_agent=_user_agent(),
        language=lang,
    )


def _article_url(language: str, title: str) -> str:
    lang = (language or "en").strip() or "en"
    path = title.strip().replace(" ", "_")
    return f"https://{lang}.wikipedia.org/wiki/{quote(path, safe='()/:%')}"


def _page_snippet(page: Any) -> str:
    meta = getattr(page, "search_meta", None)
    if meta is None:
        return ""
    snippet = getattr(meta, "snippet", None) or ""
    return str(snippet).strip()


def _run_wikipedia_search_sync(q: WikipediaQuery) -> list[dict[str, str]]:
    text = (q.query or "").strip()
    if not text:
        return []

    max_items = max(1, min(int(q.max_items or 10), 50))
    lang = (q.language or "en").strip() or "en"
    wiki = _wiki_client(lang)

    results = wiki.search(text, limit=max_items)
    pages = getattr(results, "pages", None) or {}

    out: list[dict[str, str]] = []
    for title, page in pages.items():
        title_s = str(title).strip()
        if not title_s:
            continue
        link = _article_url(lang, title_s)
        snippet = _page_snippet(page)
        if not snippet and hasattr(page, "summary"):
            try:
                snippet = (getattr(page, "summary", "") or "")[:500].strip()
            except Exception:
                snippet = ""
        out.append({"title": title_s, "link": link, "snippet": snippet})
        if len(out) >= max_items:
            break
    return out


async def wikipedia_search(q: WikipediaQuery) -> list[dict[str, str]]:
    """Async wrapper; runs blocking Wikipedia API client in a thread."""
    return await asyncio.to_thread(_run_wikipedia_search_sync, q)


async def fill_wikipedia_results(
    synapse: Any,
    *,
    query: Optional[str] = None,
    max_items: Optional[int] = None,
    language: str = "en",
) -> Any:
    """
    Set ``synapse.wikipedia_search_results`` from ``synapse.prompt`` / ``max_items`` by default.
    """
    q = query if query is not None else getattr(synapse, "prompt", "") or ""
    n = max_items if max_items is not None else getattr(synapse, "max_items", None)
    n = int(n or 10)
    rows = await wikipedia_search(
        WikipediaQuery(query=str(q), max_items=n, language=language)
    )
    setattr(synapse, "wikipedia_search_results", rows)
    return synapse


def run_wikipedia_search_sync(
    query: str,
    *,
    max_items: int = 10,
    language: str = "en",
) -> list[dict[str, str]]:
    return _run_wikipedia_search_sync(
        WikipediaQuery(query=query, max_items=max_items, language=language)
    )
