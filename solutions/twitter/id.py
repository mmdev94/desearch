"""
Twitter **by tweet id** miner helper: ``TwitterIDSearchSynapse`` → Twexapi results.
"""

from __future__ import annotations

from typing import Any

from solutions.twitter._common import NEW_ACTOR_ID, run_actor_to_result_dicts


async def search_by_id(synapse: Any) -> list[dict]:
    tid = str(getattr(synapse, "id", "") or "").strip()
    if not tid:
        return []
    run_input = {"tweetIDs": [tid]}
    return await run_actor_to_result_dicts(NEW_ACTOR_ID, run_input)
