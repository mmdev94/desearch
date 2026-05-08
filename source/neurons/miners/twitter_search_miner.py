import time

from desearch.protocol import (
    TwitterIDSearchSynapse,
    TwitterSearchSynapse,
    TwitterURLsSearchSynapse,
)

from neurons.miners._repo_import import ensure_repo_root_on_path
from neurons.miners.worker_pool import X_SEARCH_KIND


def _dendrite_hotkey(synapse) -> str | None:
    d = getattr(synapse, "dendrite", None)
    return getattr(d, "hotkey", None) if d else None


class TwitterSearchMiner:
    def __init__(self, miner: any):
        self.miner = miner

    def _ctx(self, hk: str | None):
        uid = (
            self.miner.validator_uid_for_hotkey(hk)
            if hasattr(self.miner, "validator_uid_for_hotkey")
            else None
        )
        gate = getattr(self.miner, "concurrency_gate", None)
        return hk, uid, gate

    async def search(self, synapse: TwitterSearchSynapse):
        ensure_repo_root_on_path()
        from db.miner_request_log import safe_log_miner_request
        from solutions.twitter.query import search as solution_search

        bt.logging.info(f"Executing Twex solutions search with query: {synapse.query}")
        hk, uid, gate = self._ctx(_dendrite_hotkey(synapse))
        t0 = time.perf_counter()

        async def _body():
            try:
                synapse.results = await solution_search(synapse)
                safe_log_miner_request(
                    "x_search",
                    request_payload={
                        "kind": "twitter_search",
                        "query": synapse.query,
                        "count": getattr(synapse, "count", None),
                        "results_count": len(synapse.results or []),
                    },
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                    dendrite_hotkey=hk,
                    validator_uid=uid,
                )
                bt.logging.info(
                    f"Twitter search results count: {len(synapse.results or [])}"
                )
                return synapse
            except Exception as e:
                safe_log_miner_request(
                    "x_search",
                    request_payload={
                        "kind": "twitter_search",
                        "query": getattr(synapse, "query", ""),
                    },
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                    exc=e,
                    dendrite_hotkey=hk,
                    validator_uid=uid,
                )
                raise

        if gate:
            async with gate.acquire(X_SEARCH_KIND, hk):
                return await _body()
        return await _body()

    async def search_by_id(self, synapse: TwitterIDSearchSynapse):
        ensure_repo_root_on_path()
        from db.miner_request_log import safe_log_miner_request
        from solutions.twitter.id import search_by_id as solution_by_id

        bt.logging.info(f"Searching for tweet by ID: {synapse.id}")
        hk, uid, gate = self._ctx(_dendrite_hotkey(synapse))
        t0 = time.perf_counter()

        async def _body():
            try:
                synapse.results = await solution_by_id(synapse)
                safe_log_miner_request(
                    "x_search",
                    request_payload={
                        "kind": "twitter_id",
                        "id": synapse.id,
                        "results_count": len(synapse.results or []),
                    },
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                    dendrite_hotkey=hk,
                    validator_uid=uid,
                )
                return synapse
            except Exception as e:
                safe_log_miner_request(
                    "x_search",
                    request_payload={
                        "kind": "twitter_id",
                        "id": getattr(synapse, "id", None),
                    },
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                    exc=e,
                    dendrite_hotkey=hk,
                    validator_uid=uid,
                )
                raise

        if gate:
            async with gate.acquire(X_SEARCH_KIND, hk):
                return await _body()
        return await _body()

    async def search_by_urls(self, synapse: TwitterURLsSearchSynapse):
        ensure_repo_root_on_path()
        from db.miner_request_log import safe_log_miner_request
        from solutions.twitter.url import search_by_urls as solution_by_urls

        bt.logging.info(f"Searching for tweets by URLs: {synapse.urls}")
        hk, uid, gate = self._ctx(_dendrite_hotkey(synapse))
        t0 = time.perf_counter()

        async def _body():
            try:
                synapse.results = await solution_by_urls(synapse)
                safe_log_miner_request(
                    "x_search",
                    request_payload={
                        "kind": "twitter_urls",
                        "urls": list(synapse.urls or []),
                        "results_count": len(synapse.results or []),
                    },
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                    dendrite_hotkey=hk,
                    validator_uid=uid,
                )
                return synapse
            except Exception as e:
                safe_log_miner_request(
                    "x_search",
                    request_payload={
                        "kind": "twitter_urls",
                        "urls": list(getattr(synapse, "urls", None) or []),
                    },
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                    exc=e,
                    dendrite_hotkey=hk,
                    validator_uid=uid,
                )
                raise

        if gate:
            async with gate.acquire(X_SEARCH_KIND, hk):
                return await _body()
        return await _body()
