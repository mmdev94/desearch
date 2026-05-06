#!/usr/bin/env python3
"""Run one web synthetic task and compute validator-like scores.

Behavior:
- Loads task from ``tasks/web/<id>.json`` (id can be ``1`` or ``0001``).
- Runs current miner web solution from project root:
  ``solutions.web.search.run_web_search``.
- Runs web validation scoring from source reward/penalty modules.
- ScrapingDog link verification is bypassed and treated as pass
  by injecting one validator link sample per response.
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
    web_dir = _repo_root() / "tasks" / "web"
    web_dir.mkdir(parents=True, exist_ok=True)
    if task_id.isdigit():
        return web_dir / f"{int(task_id):04d}.json"
    return web_dir / task_id


def _build_dummy_neuron():
    return SimpleNamespace(config=SimpleNamespace(neuron=SimpleNamespace(device="cpu")))


async def _inject_scrapingdog_pass(
    responses: list[Any],
    WebSearchValidatorResult: Any,
) -> list[dict]:
    """
    Replace external validator scraping with deterministic "pass" samples.
    Mimics one random-link validator check per response.
    """
    out = [{} for _ in responses]
    for response in responses:
        if not response.results:
            continue
        sample = random.choice(response.results)
        link = sample.get("link", "")
        title = sample.get("title", "") or "Validator title"
        snippet = (sample.get("snippet", "") or "").strip()
        validator_snippet = f"{snippet} {response.query}".strip()
        html_payload = f"{title}\n{validator_snippet}"
        response.validator_links.append(
            WebSearchValidatorResult(
                title=title,
                snippet=validator_snippet,
                link=link,
                html_content=html_payload,
                html_text=html_payload,
            )
        )
    return out


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    _load_repo_env()
    _bootstrap_source_imports()

    from desearch.protocol import WebSearchSynapse, WebSearchValidatorResult
    from neurons.validators.penalty.count_penalty import CountPenaltyModel
    from neurons.validators.penalty.timeout_penalty import TimeoutPenaltyModel
    from neurons.validators.reward.performance_reward import PerformanceRewardModel
    from neurons.validators.reward.web_basic_search_content_relevance import (
        WebBasicSearchContentRelevanceModel,
    )
    from solutions.web.search import run_web_search

    task_path = _resolve_task_path(args.task)
    if not task_path.is_file():
        raise FileNotFoundError(f"Task file not found: {task_path}")
    task = json.loads(task_path.read_text(encoding="utf-8"))

    query_obj = task.get("query", {})
    prompt = str(query_obj.get("query", "")).strip()
    if not prompt:
        raise ValueError(f"Task has empty query: {task_path}")

    params = dict(query_obj)
    params.pop("query", None)
    num = int(params.get("num", args.num))
    max_execution_time = int(params.get("max_execution_time", args.max_execution_time))

    synapse = WebSearchSynapse(
        query=prompt,
        num=num,
        max_execution_time=max_execution_time,
    )

    start = time.perf_counter()
    synapse = await run_web_search(synapse)
    elapsed = time.perf_counter() - start

    synapse.results = synapse.results or []
    synapse.dendrite = {"status_code": 200, "process_time": elapsed}

    responses = [synapse]
    uids = torch.tensor([0])

    neuron = _build_dummy_neuron()
    quality_model = WebBasicSearchContentRelevanceModel(
        device="cpu",
        scoring_type=None,
        neuron=neuron,
    )
    # Bypass ScrapingDog fetch; treat random link check as passed.
    quality_model.process_links = lambda responses: _inject_scrapingdog_pass(
        responses, WebSearchValidatorResult
    )

    quality_events, quality_by_uid = await quality_model.get_rewards(responses, uids)
    quality_score = float(quality_events[0].reward if quality_events else 0.0)

    perf_model = PerformanceRewardModel(
        device="cpu",
        neuron=neuron,
        min_realistic_time=0.7,
        target_time=2.0,
    )
    perf_events, _ = await perf_model.get_rewards(responses, uids)
    performance_score = float(perf_events[0].reward if perf_events else 0.0)

    base_score = 0.70 * quality_score + 0.30 * performance_score

    timeout_penalty = TimeoutPenaltyModel(max_penalty=1, neuron=neuron)
    count_penalty = CountPenaltyModel(max_penalty=1, neuron=neuron)
    _, _, timeout_applied = await timeout_penalty.apply_penalties(responses, uids)
    _, _, count_applied = await count_penalty.apply_penalties(responses, uids)
    final_score = float(base_score * timeout_applied[0].item() * count_applied[0].item())

    return {
        "task_file": str(task_path),
        "task_query": prompt,
        "task_params": {"num": num, "max_execution_time": max_execution_time},
        "output": {
            "results_count": len(synapse.results),
            "sample_results": synapse.results[: args.show_results],
            "process_time_seconds": round(elapsed, 4),
        },
        "validation": {
            "scrapingdog_check": "BYPASSED_AS_PASS",
            "quality_score": round(quality_score, 6),
            "performance_score": round(performance_score, 6),
            "weights": {"quality": 0.70, "performance": 0.30},
            "base_weighted_score": round(base_score, 6),
            "timeout_penalty_applied": round(float(timeout_applied[0].item()), 6),
            "count_penalty_applied": round(float(count_applied[0].item()), 6),
            "final_validation_score": round(final_score, 6),
            "validator_link_score_by_uid": {
                str(k): v for k, v in (quality_by_uid or {}).items()
            },
        },
    }


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run one web task and validator-style scoring."
    )
    p.add_argument("--task", required=True, help="Task id or filename in tasks/web.")
    p.add_argument("--num", type=int, default=10, help="Requested web result count.")
    p.add_argument(
        "--max-execution-time",
        type=int,
        default=10,
        help="Timeout used by validator scoring.",
    )
    p.add_argument(
        "--show-results",
        type=int,
        default=3,
        help="How many output results to print.",
    )
    return p


def main() -> int:
    args = _build_parser().parse_args()
    report = asyncio.run(_run(args))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
