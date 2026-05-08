import time

import bittensor as bt
from desearch.protocol import WebSearchSynapse

from neurons.miners._repo_import import ensure_repo_root_on_path
from neurons.miners.worker_pool import WEB_SEARCH_KIND


def _dendrite_hotkey(synapse: WebSearchSynapse) -> str | None:
    d = getattr(synapse, "dendrite", None)
    return getattr(d, "hotkey", None) if d else None


class WebSearchMiner:
    def __init__(self, miner: any):
        self.miner = miner

    async def search(self, synapse: WebSearchSynapse):
        ensure_repo_root_on_path()
        from db.miner_request_log import safe_log_miner_request
        from solutions.web.search import SerperWebSearch

        bt.logging.info(f"Executing web search with query: {synapse.query}")
        hk = _dendrite_hotkey(synapse)
        uid = (
            self.miner.validator_uid_for_hotkey(hk)
            if hasattr(self.miner, "validator_uid_for_hotkey")
            else None
        )
        gate = getattr(self.miner, "concurrency_gate", None)
        t0 = time.perf_counter()

        async def _body():
            try:
                synapse_local = await SerperWebSearch().search(synapse)
                safe_log_miner_request(
                    "web_search",
                    request_payload={
                        "query": synapse_local.query,
                        "start": getattr(synapse_local, "start", None),
                        "num": getattr(synapse_local, "num", None),
                        "results_count": len(synapse_local.results or []),
                        "results_preview": (synapse_local.results or [])[:3],
                    },
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                    dendrite_hotkey=hk,
                    validator_uid=uid,
                )
                return synapse_local
            except Exception as e:
                safe_log_miner_request(
                    "web_search",
                    request_payload={
                        "query": getattr(synapse, "query", ""),
                        "start": getattr(synapse, "start", None),
                        "num": getattr(synapse, "num", None),
                    },
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                    exc=e,
                    dendrite_hotkey=hk,
                    validator_uid=uid,
                )
                raise

        if gate:
            async with gate.acquire(WEB_SEARCH_KIND, hk):
                return await _body()
        return await _body()
