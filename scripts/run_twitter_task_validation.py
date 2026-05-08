#!/usr/bin/env python3
"""Run one x_search synthetic task and compute validator-like scores.

Behavior:
- Loads task from ``tasks/x/<id>.json`` (id can be ``1`` or ``0001``).
- Runs current Twitter miner solution: ``solutions.twitter.search``.
- Runs X validation scoring from source (same weights/perf curve as ``XScraperValidator``).
- Validator-side tweet re-fetch in
  ``TwitterBasicSearchContentRelevanceModel.process_tweets`` is bypassed and treated
  as pass by injecting ``validator_tweets`` from the same sampled miner row
  (so content checks align without a second network scrape).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import torch

_TWITTER_SYNAPSE_FIELDS = frozenset(
    {
        "sort",
        "user",
        "count",
        "start_date",
        "end_date",
        "lang",
        "verified",
        "blue_verified",
        "is_quote",
        "is_video",
        "is_image",
        "min_retweets",
        "min_replies",
        "min_likes",
    }
)


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
    x_dir = _repo_root() / "tasks" / "x"
    x_dir.mkdir(parents=True, exist_ok=True)
    if task_id.isdigit():
        return x_dir / f"{int(task_id):04d}.json"
    return x_dir / task_id


def _build_dummy_neuron():
    return SimpleNamespace(config=SimpleNamespace(neuron=SimpleNamespace(device="cpu")))


def _calc_max_execution_time(count: int | None) -> int:
    """Match ``XScraperValidator.calc_max_execution_time``."""
    if not count or count <= 20:
        return 10
    return 10 + int((count - 20) / 20) * 5


async def _inject_validator_tweets_from_miner_sample(
    responses: list[Any],
    TwitterScraperTweet: Any,
) -> list[dict]:
    """Skip external re-scrape; treat sampled miner tweet as validator ground truth."""
    out: list[dict] = [{} for _ in responses]
    for response in responses:
        if not response.results:
            continue
        sample = random.choice(response.results)
        try:
            vt = TwitterScraperTweet.model_validate(sample)
        except Exception:
            continue
        response.validator_tweets.append(vt)
    return out


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    _load_repo_env()
    _bootstrap_source_imports()

    from desearch.protocol import TwitterSearchSynapse, TwitterScraperTweet
    from neurons.validators.penalty.count_penalty import CountPenaltyModel
    from neurons.validators.penalty.timeout_penalty import TimeoutPenaltyModel
    from neurons.validators.reward import RewardScoringType
    from neurons.validators.reward.performance_reward import PerformanceRewardModel
    from neurons.validators.reward.twitter_basic_search_content_relevance import (
        TwitterBasicSearchContentRelevanceModel,
    )
    from solutions.twitter import search as twitter_search

    task_path = _resolve_task_path(args.task)
    if not task_path.is_file():
        raise FileNotFoundError(f"Task file not found: {task_path}")
    task = json.loads(task_path.read_text(encoding="utf-8"))

    if task.get("search_type") not in (None, "x_search"):
        raise ValueError(
            f"Expected search_type x_search, got {task.get('search_type')!r} in {task_path}"
        )

    query_obj = task.get("query", {})
    prompt = str(query_obj.get("query", "")).strip()
    if not prompt:
        raise ValueError(f"Task has empty query: {task_path}")

    synapse_kwargs = {
        k: v for k, v in query_obj.items() if k in _TWITTER_SYNAPSE_FIELDS and k != "query"
    }
    count = int(synapse_kwargs.get("count", args.count))
    synapse_kwargs["count"] = count
    max_execution_time = _calc_max_execution_time(count)
    if "max_execution_time" in query_obj and query_obj["max_execution_time"] is not None:
        max_execution_time = int(query_obj["max_execution_time"])

    synapse = TwitterSearchSynapse(
        query=prompt,
        max_execution_time=max_execution_time,
        **synapse_kwargs,
    )

    start = time.perf_counter()
    synapse.results = await twitter_search(synapse) or []
    elapsed = time.perf_counter() - start

    synapse.dendrite = {"status_code": 200, "process_time": elapsed}

    responses = [synapse]
    uids = torch.tensor([0])

    neuron = _build_dummy_neuron()
    quality_model = TwitterBasicSearchContentRelevanceModel(
        device="cpu",
        scoring_type=RewardScoringType.search_relevance_score_template,
        neuron=neuron,
    )
    quality_model.process_tweets = lambda responses: _inject_validator_tweets_from_miner_sample(
        responses, TwitterScraperTweet
    )

    quality_events, quality_by_uid = await quality_model.get_rewards(responses, uids)
    quality_score = float(quality_events[0].reward if quality_events else 0.0)

    perf_model = PerformanceRewardModel(
        device="cpu",
        neuron=neuron,
        min_realistic_time=1.0,
        target_time=3.0,
    )
    perf_events, _ = await perf_model.get_rewards(responses, uids)
    performance_score = float(perf_events[0].reward if perf_events else 0.0)

    twitter_content_weight = 0.70
    performance_weight = 0.30
    base_score = twitter_content_weight * quality_score + performance_weight * performance_score

    timeout_penalty = TimeoutPenaltyModel(max_penalty=1, neuron=neuron)
    count_penalty = CountPenaltyModel(max_penalty=1, neuron=neuron)
    _, _, timeout_applied = await timeout_penalty.apply_penalties(responses, uids)
    _, _, count_applied = await count_penalty.apply_penalties(responses, uids)
    final_score = float(base_score * timeout_applied[0].item() * count_applied[0].item())

    sample = synapse.results[: args.show_results]

    return {
        "task_file": str(task_path),
        "task_query": prompt,
        "task_params": {
            k: v for k, v in synapse_kwargs.items()
        }
        | {"max_execution_time": max_execution_time},
        "output": {
            "results_count": len(synapse.results),
            "sample_results": sample,
            "process_time_seconds": round(elapsed, 4),
        },
        "validation": {
            "external_rescrape_check": "BYPASSED_AS_PASS",
            "quality_score": round(quality_score, 6),
            "performance_score": round(performance_score, 6),
            "weights": {
                "twitter_content": twitter_content_weight,
                "performance": performance_weight,
            },
            "base_weighted_score": round(base_score, 6),
            "timeout_penalty_applied": round(float(timeout_applied[0].item()), 6),
            "count_penalty_applied": round(float(count_applied[0].item()), 6),
            "final_validation_score": round(final_score, 6),
            "validator_tweet_score_by_uid": {
                str(k): v for k, v in (quality_by_uid or {}).items()
            },
        },
    }


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run one x_search task and validator-style scoring."
    )
    p.add_argument("--task", required=True, help="Task id or filename in tasks/x.")
    p.add_argument(
        "--count",
        type=int,
        default=20,
        help="Default TwitterSearchSynapse.count if omitted in task JSON.",
    )
    p.add_argument(
        "--show-results",
        type=int,
        default=3,
        help="How many tweet dicts to include in report sample.",
    )
    return p


def main() -> int:
    args = _build_parser().parse_args()
    report = asyncio.run(_run(args))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
