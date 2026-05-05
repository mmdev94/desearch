"""Twikit fetch helper: ``TwitterURLsSearchSynapse``-like input -> tweet dict results."""

from __future__ import annotations

from typing import Any

from solutions.twitter1._common import tweets_by_urls


async def search_by_urls(synapse: Any) -> list[dict]:
    urls = list(getattr(synapse, "urls", None) or [])
    if not urls:
        return []
    return await tweets_by_urls(urls)
