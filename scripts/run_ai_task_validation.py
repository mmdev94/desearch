#!/usr/bin/env python3
"""Run one AI task and validator-style scoring (link check bypassed)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import torch


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _bootstrap_source_imports() -> None:
    repo_root = _repo_root()
    source_root = repo_root / "source"
    for p in (repo_root, source_root):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))


def _load_repo_env() -> None:
    env_path = _repo_root() / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        os.environ[key] = value


def _resolve_task_path(task_id: str) -> Path:
    ai_dir = _repo_root() / "tasks" / "ai"
    if task_id.isdigit():
        return ai_dir / f"{int(task_id):04d}.json"
    return ai_dir / task_id


def _build_dummy_neuron():
    return SimpleNamespace(config=SimpleNamespace(neuron=SimpleNamespace(device="cpu")))


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    _load_repo_env()
    _bootstrap_source_imports()

    from neurons.validators.penalty.streaming_penalty import StreamingPenaltyModel
    from neurons.validators.penalty.timeout_penalty import TimeoutPenaltyModel
    from neurons.validators.reward.performance_reward import PerformanceRewardModel
    from neurons.validators.reward.reward_llm import RewardLLM
    from neurons.validators.reward.summary_relevance import SummaryRelevanceRewardModel
    from solutions.ai.ai import build_ai_synapse_from_task, run_ai_solution

    task_path = _resolve_task_path(args.task)
    if not task_path.is_file():
        raise FileNotFoundError(f"Task file not found: {task_path}")
    task = json.loads(task_path.read_text(encoding="utf-8"))

    synapse = build_ai_synapse_from_task(task)
    start = time.perf_counter()
    synapse = await run_ai_solution(synapse, task_meta=task)
    elapsed = time.perf_counter() - start
    synapse.dendrite = {"status_code": 200, "process_time": elapsed}

    responses = [synapse]
    uids = torch.tensor([0])
    neuron = _build_dummy_neuron()

    llm_reward = RewardLLM()
    summary_model = SummaryRelevanceRewardModel(
        device="cpu",
        scoring_type=None,
        llm_reward=llm_reward,
        neuron=neuron,
    )
    summary_model.verify_link_sources = lambda response, links: (
        len(links),
        len(links),
        {u: True for u in links},
    )
    summary_events, summary_details = await summary_model.get_rewards(responses, uids)
    summary_score = float(summary_events[0].reward if summary_events else 0.0)

    perf_model = PerformanceRewardModel(
        device="cpu",
        neuron=neuron,
        min_realistic_time=0.7,
        target_time=2.0,
    )
    perf_events, _ = await perf_model.get_rewards(responses, uids)
    performance_score = float(perf_events[0].reward if perf_events else 0.0)

    timeout_penalty = TimeoutPenaltyModel(max_penalty=1, neuron=neuron)
    _, _, timeout_applied = await timeout_penalty.apply_penalties(responses, uids)

    streaming_penalty = StreamingPenaltyModel(max_penalty=1, neuron=neuron)
    _, _, streaming_applied = await streaming_penalty.apply_penalties(responses, uids)

    base_score = 0.75 * summary_score + 0.25 * performance_score
    final_score = float(
        base_score
        * timeout_applied[0].item()
        * streaming_applied[0].item()
    )

    return {
        "task_file": str(task_path),
        "task_query": synapse.prompt,
        "tools": synapse.tools,
        "output": {
            "twitter_count": len(synapse.miner_tweets or []),
            "web_count": len(synapse.search_results or []),
            "reddit_count": len(synapse.reddit_search_results or []),
            "youtube_count": len(synapse.youtube_search_results or []),
            "arxiv_count": len(synapse.arxiv_search_results or []),
            "hackernews_count": len(synapse.hacker_news_search_results or []),
            "wikipedia_count": len(synapse.wikipedia_search_results or []),
            "process_time_seconds": round(elapsed, 4),
            "summary_preview": synapse.texts.get("final_summary", "")[:1200],
        },
        "validation": {
            "link_check": "BYPASSED_AS_REQUESTED",
            "summary_relevance_score": round(summary_score, 6),
            "performance_score": round(performance_score, 6),
            "weights": {"summary_relevance": 0.75, "performance": 0.25},
            "base_weighted_score": round(base_score, 6),
            "timeout_penalty_applied": round(float(timeout_applied[0].item()), 6),
            "streaming_penalty_applied": round(float(streaming_applied[0].item()), 6),
            "final_validation_score": round(final_score, 6),
            "summary_scoring_details": summary_details,
        },
    }


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run one AI task validation.")
    p.add_argument("--task", required=True, help="Task id or filename in tasks/ai.")
    return p


def main() -> int:
    args = _build_parser().parse_args()
    report = asyncio.run(_run(args))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
