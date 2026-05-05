"""Twikit fetch helper: ``TwitterIDSearchSynapse``-like input -> tweet dict results."""

from __future__ import annotations

from typing import Any

from solutions.twitter1._common import tweets_by_ids


async def search_by_id(synapse: Any) -> list[dict]:
    tid = str(getattr(synapse, "id", "") or "").strip()
    if not tid:
        return []
    return await tweets_by_ids([tid])
