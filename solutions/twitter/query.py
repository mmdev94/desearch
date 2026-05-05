"""
Twitter **search** miner helper: ``TwitterSearchSynapse`` → Apify results.

Uses the **new** actor (``CJdippxWmn9uRfooo``) so query, ID, and URL paths align
with validator ``get_tweets``.
"""

from __future__ import annotations

from typing import Any

from solutions.twitter._common import (
    NEW_ACTOR_ID,
    new_actor_search_input_from_synapse,
    run_actor_to_result_dicts,
)


async def search(
    synapse: Any,
) -> list[dict]:
    """
    Run Apify Twitter search from a ``TwitterSearchSynapse``-like object and return
    tweet dicts (same shape as ``TwitterScraperTweet.model_dump(mode='json')``).

    Uses actor ``CJdippxWmn9uRfooo``.
    """
    run_input = new_actor_search_input_from_synapse(synapse)
    return await run_actor_to_result_dicts(NEW_ACTOR_ID, run_input)
