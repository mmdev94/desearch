"""
ArXiv search via ``arxiv`` PyPI package (lukasschwab/arxiv.py).

Do **not** name this module ``arxiv.py`` — it would shadow ``import arxiv`` and break.

This helper populates ``ScraperStreamingSynapse.arxiv_search_results`` with
``SearchResultItem``-shaped dicts: ``{title, link, snippet}``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal, Optional

import arxiv


SortBy = Literal["relevance", "submitted_date", "last_updated_date"]
SortOrder = Literal["ascending", "descending"]

_SORT_BY_MAP = {
    "relevance": arxiv.SortCriterion.Relevance,
    "submitted_date": arxiv.SortCriterion.SubmittedDate,
    "last_updated_date": arxiv.SortCriterion.LastUpdatedDate,
}

_SORT_ORDER_MAP = {
    "ascending": arxiv.SortOrder.Ascending,
    "descending": arxiv.SortOrder.Descending,
}


@dataclass(frozen=True)
class ArxivQuery:
    query: str
    max_items: int = 10
    sort_by: SortBy = "relevance"
    sort_order: SortOrder = "descending"
    id_list: Optional[list[str]] = None


def _result_to_search_item(result: arxiv.Result) -> dict[str, str]:
    return {
        "title": (result.title or "").strip(),
        "link": str(result.entry_id or result.pdf_url or "").strip(),
        "snippet": (result.summary or "").strip(),
    }


def _run_arxiv_search_sync(q: ArxivQuery) -> list[dict[str, str]]:
    query = (q.query or "").strip()
    ids = [str(i).strip() for i in (q.id_list or []) if str(i).strip()]
    if not query and not ids:
        return []

    max_items = max(1, min(int(q.max_items or 10), 100))
    sort_by = _SORT_BY_MAP.get(q.sort_by, arxiv.SortCriterion.Relevance)
    sort_order = _SORT_ORDER_MAP.get(q.sort_order, arxiv.SortOrder.Descending)

    search = arxiv.Search(
        query=query,
        id_list=ids,
        max_results=max_items,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    client = arxiv.Client()

    out: list[dict[str, str]] = []
    for r in client.results(search):
        item = _result_to_search_item(r)
        if item["title"] and item["link"]:
            out.append(item)
        if len(out) >= max_items:
            break
    return out


async def arxiv_search(q: ArxivQuery) -> list[dict[str, str]]:
    """Return a list of ``SearchResultItem`` dicts for arXiv."""
    return await asyncio.to_thread(_run_arxiv_search_sync, q)


async def fill_arxiv_results(
    synapse: Any,
    *,
    query: Optional[str] = None,
    max_items: Optional[int] = None,
    sort_by: SortBy = "relevance",
    sort_order: SortOrder = "descending",
    id_list: Optional[list[str]] = None,
) -> Any:
    """
    Populate ``synapse.arxiv_search_results`` in-place and return synapse.

    Defaults:
    - query from ``synapse.prompt``
    - max_items from ``synapse.max_items`` (fallback 10)
    """
    q = query if query is not None else getattr(synapse, "prompt", "") or ""
    n = max_items if max_items is not None else getattr(synapse, "max_items", None)
    n = int(n or 10)
    results = await arxiv_search(
        ArxivQuery(
            query=str(q),
            max_items=n,
            sort_by=sort_by,
            sort_order=sort_order,
            id_list=id_list,
        )
    )
    setattr(synapse, "arxiv_search_results", results)
    return synapse


def run_arxiv_search_sync(
    query: str,
    *,
    max_items: int = 10,
    sort_by: SortBy = "relevance",
    sort_order: SortOrder = "descending",
    id_list: Optional[list[str]] = None,
) -> list[dict[str, str]]:
    """Synchronous wrapper for quick CLI/manual testing."""
    return _run_arxiv_search_sync(
        ArxivQuery(
            query=query,
            max_items=max_items,
            sort_by=sort_by,
            sort_order=sort_order,
            id_list=id_list,
        )
    )
