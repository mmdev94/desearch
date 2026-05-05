"""Twikit search helper: ``TwitterSearchSynapse``-like input -> tweet dict results."""

from __future__ import annotations

from typing import Any

from solutions.twitter1._common import build_twikit_query_from_synapse, search_tweets


async def search(synapse: Any, *, proxy: str | None = None) -> list[dict]:
    query = build_twikit_query_from_synapse(synapse)
    sort = getattr(synapse, "sort", None) or "Latest"
    count = int(getattr(synapse, "count", 20) or 20)
    return await search_tweets(query=query, product=sort, count=count, proxy=proxy)
