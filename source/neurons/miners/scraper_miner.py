import json
import time
import traceback

import bittensor as bt
from starlette.types import Send

from desearch.protocol import ScraperStreamingSynapse, ScraperTextRole
from desearch.tools.response_streamer import ResponseStreamer

from neurons.miners._repo_import import ensure_repo_root_on_path
from neurons.miners.worker_pool import AI_SEARCH_KIND


def _dendrite_hotkey(synapse: ScraperStreamingSynapse) -> str | None:
    d = getattr(synapse, "dendrite", None)
    return getattr(d, "hotkey", None) if d else None


def _serialize_link_scores(raw: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in (raw or {}).items():
        out[str(k)] = v.value if hasattr(v, "value") else str(v)
    return out


def _plain_items(items: list | None) -> list:
    if not items:
        return []
    out: list = []
    for x in items:
        if hasattr(x, "model_dump"):
            out.append(x.model_dump(mode="json"))
        elif isinstance(x, dict):
            out.append(x)
        else:
            out.append(json.loads(json.dumps(x, default=str)))
    return out


async def _emit_solution_stream(send: Send, synapse: ScraperStreamingSynapse) -> None:
    """Replay validator-facing SSE JSON chunks after ``run_ai_solution`` filled ``synapse``."""
    rs = ResponseStreamer(send)

    if synapse.miner_tweets:
        await rs.send_event("tweets", synapse.miner_tweets)

    if synapse.search_results:
        await rs.send_event("search", _plain_items(synapse.search_results))

    if synapse.wikipedia_search_results:
        await rs.send_event(
            "wikipedia_search",
            _plain_items(synapse.wikipedia_search_results),
        )
    if synapse.youtube_search_results:
        await rs.send_event(
            "youtube_search",
            _plain_items(synapse.youtube_search_results),
        )
    if synapse.arxiv_search_results:
        await rs.send_event(
            "arxiv_search",
            _plain_items(synapse.arxiv_search_results),
        )
    if synapse.reddit_search_results:
        await rs.send_event(
            "reddit_search",
            _plain_items(synapse.reddit_search_results),
        )
    if synapse.hacker_news_search_results:
        await rs.send_event(
            "hacker_news_search",
            _plain_items(synapse.hacker_news_search_results),
        )

    if synapse.miner_link_scores:
        await rs.send_event(
            "miner_link_scores",
            _serialize_link_scores(dict(synapse.miner_link_scores)),
        )

    chunks = (synapse.text_chunks or {}).get(
        ScraperTextRole.FINAL_SUMMARY.value, []
    ) or []
    for piece in chunks:
        await rs.send_text_event(str(piece), ScraperTextRole.FINAL_SUMMARY)

    await rs.send_completion_event()


class ScraperMiner:
    def __init__(self, miner: any):
        self.miner = miner

    async def smart_scraper(self, synapse: ScraperStreamingSynapse, send: Send):
        ensure_repo_root_on_path()
        from db.miner_request_log import safe_log_miner_request
        from solutions.ai.ai import run_ai_solution

        t0 = time.perf_counter()
        hk = _dendrite_hotkey(synapse)
        uid = (
            self.miner.validator_uid_for_hotkey(hk)
            if hasattr(self.miner, "validator_uid_for_hotkey")
            else None
        )
        gate = getattr(self.miner, "concurrency_gate", None)

        async def _body() -> None:
            try:
                bt.logging.trace(synapse)
                bt.logging.info(
                    "================================== Prompt ==================================="
                )
                bt.logging.info(synapse.prompt)
                bt.logging.info(
                    "================================== Prompt ===================================="
                )

                synapse = await run_ai_solution(synapse)
                await _emit_solution_stream(send, synapse)

                safe_log_miner_request(
                    "ai_search",
                    request_payload={
                        "prompt": synapse.prompt,
                        "tools": synapse.tools,
                        "completion": synapse.completion,
                        "result_type": getattr(
                            synapse.result_type, "value", synapse.result_type
                        ),
                        "miner_link_scores": _serialize_link_scores(
                            dict(synapse.miner_link_scores or {})
                        ),
                        "summary_preview": "".join(
                            (synapse.text_chunks or {}).get(
                                ScraperTextRole.FINAL_SUMMARY.value, []
                            )
                            or []
                        )[:8000],
                    },
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                    exc=None,
                    dendrite_hotkey=hk,
                    validator_uid=uid,
                )
                bt.logging.info("End of Streaming (solutions ai)")
            except Exception as e:
                bt.logging.error(f"error in scraper miner {e}\n{traceback.format_exc()}")
                safe_log_miner_request(
                    "ai_search",
                    request_payload={
                        "prompt": getattr(synapse, "prompt", ""),
                        "tools": getattr(synapse, "tools", None),
                    },
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                    exc=e,
                    dendrite_hotkey=hk,
                    validator_uid=uid,
                )

        if gate:
            async with gate.acquire(AI_SEARCH_KIND, hk):
                await _body()
        else:
            await _body()
