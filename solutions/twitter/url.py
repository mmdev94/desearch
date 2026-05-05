"""
Twitter **by status URLs** miner helper: ``TwitterURLsSearchSynapse`` → Apify results.

Extracts tweet ids with ``TwitterUtils.extract_tweet_id`` and calls the **new** actor
with ``tweetIDs`` (same as ``TwitterScraperActor.get_tweets``).
"""

from __future__ import annotations

from typing import Any

from solutions.twitter._common import (
    NEW_ACTOR_ID,
    ensure_desearch_importable,
    run_actor_to_result_dicts,
)


async def search_by_urls(synapse: Any) -> list[dict]:
    ensure_desearch_importable()
    from desearch.services.twitter_utils import TwitterUtils

    urls = getattr(synapse, "urls", None) or []
    tweet_ids = [TwitterUtils.extract_tweet_id(u) for u in urls]
    tweet_ids = [t for t in tweet_ids if t]
    if not tweet_ids:
        return []
    run_input = {"tweetIDs": tweet_ids}
    return await run_actor_to_result_dicts(NEW_ACTOR_ID, run_input)
