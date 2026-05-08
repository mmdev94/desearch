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

TWITTER_TOOL = "Twitter Search"
WEB_TOOLS = frozenset(
    [
        "Web Search",
        "Wikipedia Search",
        "Youtube Search",
        "ArXiv Search",
        "Reddit Search",
        "Hacker News Search",
    ]
)

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _bootstrap_source_imports() -> None:
    repo_root = _repo_root()
    source_root = repo_root / "source"
    for p in (repo_root, source_root):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))


def _per_link_domain_gate(synapse, url: str) -> tuple[bool, str]:
    """Mirror WebSearchContentRelevanceModel.check_response_random_link per-URL logic."""
    if not url:
        return False, "empty_url"
    try:
        domain_parts = url.split("/")[2].split(".")
        domain = ".".join(domain_parts[-2:])
    except IndexError:
        return False, "unparseable_url"
    web_search_results = str(synapse.search_results)
    domain_to_search_result = {
        "arxiv.org": synapse.arxiv_search_results,
        "wikipedia.org": synapse.wikipedia_search_results,
        "reddit.com": synapse.reddit_search_results,
        "ycombinator.com": synapse.hacker_news_search_results,
        "youtube.com": synapse.youtube_search_results,
    }
    if domain in domain_to_search_result:
        if url in str(domain_to_search_result[domain]) or url in web_search_results:
            return True, "domain_bucket_match"
        return False, "url_missing_from_domain_bucket_and_general_web_results"
    if url in web_search_results:
        return True, "listed_in_general_web_results_str"
    return False, "url_not_found_in_miner_web_search_results_repr"


def _diagnose_search_content_relevance_zero(
    synapse,
    web_model,
    web_grouped_scores: dict[str, Any],
) -> dict[str, Any]:
    """Explain why WebSearchContentRelevanceModel reward is 0 (validator parity fields)."""
    out: dict[str, Any] = {"reasons": []}

    completion = web_model.get_successful_search_summary_completion(synapse)
    out["completion_gate_ok"] = bool(completion)
    if not completion:
        out["reasons"].append(
            "completion_gate_false: need status 200 and non-empty get_links_from_search_results()"
        )

    search_links, links_per_group = synapse.get_links_from_search_results()
    out["miner_link_count"] = len(search_links or [])
    out["links_per_tool_group_sizes"] = {
        str(k): len(v or []) for k, v in (links_per_group or {}).items()
    }
    if len(search_links or []) < 2:
        out["reasons"].append(
            "fewer_than_two_miner_links: check_response_random_link returns 0 "
            "(validator requires >=2 search links)"
        )

    vlinks = list(synapse.validator_links or [])
    out["validator_link_sample_count"] = len(vlinks)
    if not vlinks:
        out["reasons"].append(
            "no_validator_links_sampled: nothing_to_score_for_web_content_relevance"
        )

    mult = float(web_model.check_response_random_link(synapse) or 0.0)
    out["check_response_random_link_multiplier"] = mult
    if mult == 0.0:
        out["reasons"].append(
            "check_response_random_link_is_zero: final_reward_is_multiplied_by_this "
            "(domain/url consistency with miner search payload)"
        )

    per_link = []
    for row in vlinks:
        u = (row or {}).get("link") or ""
        ok, detail = _per_link_domain_gate(synapse, str(u))
        per_link.append({"link": u[:200], "domain_gate_ok": ok, "detail": detail})
    out["validator_links_domain_gate"] = per_link

    scores_map = dict(web_grouped_scores or {})
    out["extracted_llm_scores_by_url"] = scores_map
    if not scores_map:
        out["reasons"].append(
            "no_extracted_scores: empty_val_score_responses_or_llm_parse_failed"
        )
    else:
        total = sum(float(v or 0) for v in scores_map.values())
        if total <= 0:
            out["reasons"].append(
                "all_link_level_extracted_scores_are_zero: llm_marked_irrelevant_or_unparseable"
            )

    return out


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
    flow_start = time.perf_counter()
    _load_repo_env()
    _bootstrap_source_imports()
    if args.twitter_log:
        # Enables detailed provider logs in solutions/twitter/_common.py,
        # including request/response previews and normalized payload output.
        os.environ["TWITTER_DEBUG_LOG"] = "1"
        os.environ["TWEX_DEBUG_LOG"] = "1"

    from neurons.validators.penalty.miner_score_penalty import MinerScorePenaltyModel
    from neurons.validators.penalty.streaming_penalty import StreamingPenaltyModel
    from neurons.validators.penalty.timeout_penalty import TimeoutPenaltyModel
    from neurons.validators.reward.performance_reward import PerformanceRewardModel
    from neurons.validators.reward.reward import BaseRewardEvent
    from neurons.validators.reward.reward_llm import RewardLLM
    from neurons.validators.reward.search_content_relevance import (
        WebSearchContentRelevanceModel,
    )
    from neurons.validators.reward.summary_relevance import SummaryRelevanceRewardModel
    from neurons.validators.reward.twitter_content_relevance import (
        TwitterContentRelevanceModel,
    )
    from desearch.protocol import Model
    from desearch.protocol import TwitterScraperTweet
    from desearch.utils import get_max_execution_time
    from solutions.ai.ai import build_ai_synapse_from_task, run_ai_solution

    task_path = _resolve_task_path(args.task)
    if not task_path.is_file():
        raise FileNotFoundError(f"Task file not found: {task_path}")
    task = json.loads(task_path.read_text(encoding="utf-8"))

    synapse = build_ai_synapse_from_task(task)
    # Match AdvancedScraperValidator.send_scoring_query:
    # max_execution_time = get_max_execution_time(Model.NOVA, 10)
    synapse.max_execution_time = get_max_execution_time(Model.NOVA, 10)
    start = time.perf_counter()
    synapse = await run_ai_solution(synapse, task_meta=task)
    elapsed = time.perf_counter() - start
    synapse.dendrite = {"status_code": 200, "process_time": elapsed}

    responses = [synapse]
    uids = torch.tensor([0])
    neuron = _build_dummy_neuron()

    llm_reward = RewardLLM()
    reward_models = [
        TwitterContentRelevanceModel(
            device="cpu",
            scoring_type=None,
            llm_reward=llm_reward,
            neuron=neuron,
        ),
        WebSearchContentRelevanceModel(
            device="cpu",
            scoring_type=None,
            llm_reward=llm_reward,
            neuron=neuron,
        ),
        SummaryRelevanceRewardModel(
            device="cpu",
            scoring_type=None,
            llm_reward=llm_reward,
            neuron=neuron,
        ),
        PerformanceRewardModel(
            device="cpu",
            neuron=neuron,
            min_realistic_time=5.0,
            target_time=10.0,
        ),
    ]

    # Keep prompt-based relevance scoring, but source validator items from miner
    # outputs (no external scraping).
    async def _twitter_process_tweets_from_miner(responses):
        default_val_score_responses = [{} for _ in responses]
        val_score_responses_list = []
        for response in responses:
            response.validator_tweets = []
            miner_tweets = list(response.miner_tweets or [])
            # mimic validator sample size
            sample = miner_tweets[:3]
            for tweet in sample:
                # Keep only fields used by llm_process_validator_tweets and
                # downstream checks (id/text/created_at).
                tid = str(tweet.get("id") or "").strip()
                text = str(tweet.get("text") or "").strip()
                created_at = str(tweet.get("created_at") or "").strip()
                if tid and text and created_at:
                    response.validator_tweets.append(
                        TwitterScraperTweet(
                            user=None,
                            id=tid,
                            text=text,
                            reply_count=int(tweet.get("reply_count") or 0),
                            view_count=int(tweet.get("view_count") or 0)
                            if tweet.get("view_count") is not None
                            else None,
                            retweet_count=int(tweet.get("retweet_count") or 0),
                            like_count=int(tweet.get("like_count") or 0),
                            quote_count=int(tweet.get("quote_count") or 0),
                            bookmark_count=int(tweet.get("bookmark_count") or 0),
                            url=str(tweet.get("url") or ""),
                            created_at=created_at,
                            media=[],
                            is_quote_tweet=bool(tweet.get("is_quote_tweet"))
                            if tweet.get("is_quote_tweet") is not None
                            else None,
                            is_retweet=bool(tweet.get("is_retweet"))
                            if tweet.get("is_retweet") is not None
                            else None,
                            lang=str(tweet.get("lang") or "") or None,
                            conversation_id=str(tweet.get("conversation_id") or "") or None,
                            in_reply_to_screen_name=str(
                                tweet.get("in_reply_to_screen_name") or ""
                            )
                            or None,
                            in_reply_to_status_id=str(
                                tweet.get("in_reply_to_status_id") or ""
                            )
                            or None,
                            in_reply_to_user_id=str(tweet.get("in_reply_to_user_id") or "")
                            or None,
                            quoted_status_id=str(tweet.get("quoted_status_id") or "")
                            or None,
                            quote=None,
                            replies=None,
                            display_text_range=tweet.get("display_text_range"),
                            entities=None,
                            extended_entities=None,
                        )
                    )
            # Run original prompt-based scoring over validator_tweets text.
            val_score_responses = await reward_models[0].llm_process_validator_tweets(
                response
            )
            val_score_responses_list.append(val_score_responses or {})
        return val_score_responses_list or default_val_score_responses

    def _twitter_check_content_no_scrape(response):
        # No external scrape consistency check; treat source presence as pass.
        return 1.0 if len(response.validator_tweets or []) > 0 else 0.0

    async def _web_process_links_from_miner(responses):
        default_val_score_responses = [{} for _ in responses]
        attempted_counts: list[int] = []
        val_score_responses_list = []
        for response in responses:
            response.validator_links = []
            # Build validator links from miner search outputs, preserving title/snippet.
            _, links_per_tool_group = response.get_links_from_search_results()
            for tool_group_links in links_per_tool_group.values():
                if not tool_group_links:
                    continue
                # mirror validator sampling behavior: 2 if single group else 1 each
                limit = 2 if len(links_per_tool_group) == 1 else 1
                for link in tool_group_links[:limit]:
                    row = {"link": link, "title": "", "snippet": ""}
                    for field in (
                        response.search_results
                        + response.wikipedia_search_results
                        + response.youtube_search_results
                        + response.arxiv_search_results
                        + response.reddit_search_results
                        + response.hacker_news_search_results
                    ):
                        if isinstance(field, dict) and field.get("link") == link:
                            row["title"] = str(field.get("title") or "")
                            row["snippet"] = str(field.get("snippet") or "")
                            break
                    response.validator_links.append(row)
            attempted_counts.append(len(response.validator_links))
            # Run original prompt-based scoring over title/snippet.
            val_score_responses = await reward_models[1].llm_process_validator_links(
                response
            )
            # Preserve URL-keyed map expected downstream.
            val_score_responses_list.append(val_score_responses or {})
        return (val_score_responses_list or default_val_score_responses), attempted_counts

    reward_models[0].process_tweets = _twitter_process_tweets_from_miner
    reward_models[0].check_tweet_content = _twitter_check_content_no_scrape
    reward_models[1].process_links = _web_process_links_from_miner
    reward_models[2].verify_link_sources = lambda response, links: (
        len(links),
        len(links),
        {u: True for u in links},
    )

    # Same weight logic as AdvancedScraperValidator._weights_for.
    tools = set(synapse.tools or [])
    has_twitter = TWITTER_TOOL in tools
    has_web = bool(tools & WEB_TOOLS)
    twitter_content_weight = 0.30
    web_search_weight = 0.25
    summary_relevance_weight = 0.25
    performance_weight = 0.20
    content = twitter_content_weight + web_search_weight
    if has_twitter and has_web:
        w_twitter, w_web = twitter_content_weight, web_search_weight
    elif has_twitter:
        w_twitter, w_web = content, 0.0
    else:
        w_twitter, w_web = 0.0, content
    weights = [w_twitter, w_web, summary_relevance_weight, performance_weight]

    # Compute rewards exactly in weighted-sum style used by BaseScraperValidator.
    model_scores: dict[str, float] = {}
    val_score_responses_list: list[Any] = []
    rewards = torch.zeros(len(responses), dtype=torch.float32)
    reward_stage_start = time.perf_counter()
    for i, model in enumerate(reward_models):
        reward_i, _reward_event, val_score_responses, _original_rewards = await model.apply(
            responses, uids
        )
        rewards += float(weights[i]) * reward_i
        model_scores[str(model.name)] = float(reward_i[0].item()) if len(reward_i) else 0.0
        val_score_responses_list.append(val_score_responses)
    reward_stage_elapsed = time.perf_counter() - reward_stage_start

    web_grouped_scores: dict[str, Any] = {}
    if len(val_score_responses_list) > 1 and val_score_responses_list[1]:
        web_grouped_scores = val_score_responses_list[1][0] or {}

    search_content_zero_diagnostic: dict[str, Any] | None = None
    if float(model_scores.get("search_content_relevance") or 0) <= 0:
        search_content_zero_diagnostic = _diagnose_search_content_relevance_zero(
            synapse, reward_models[1], web_grouped_scores
        )
        print(
            "[ai-validation-search-content-zero] reasons="
            + json.dumps(
                search_content_zero_diagnostic.get("reasons", []),
                ensure_ascii=False,
            )
        )

    # Same penalty additional params as AdvancedScraperValidator.get_penalty_additional_params.
    penalty_additional_params = []
    for val_score_responses, model in zip(val_score_responses_list, reward_models):
        if str(model.name) in ["twitter_content_relevance", "search_content_relevance"]:
            penalty_additional_params.append(val_score_responses)

    penalty_models = [
        StreamingPenaltyModel(max_penalty=1, neuron=neuron),
        TimeoutPenaltyModel(max_penalty=1, neuron=neuron),
        MinerScorePenaltyModel(max_penalty=1, neuron=neuron),
    ]
    penalty_applied_map: dict[str, float] = {}
    penalty_raw_map: dict[str, float] = {}
    penalty_adjusted_map: dict[str, float] = {}
    penalty_stage_start = time.perf_counter()
    for penalty_model in penalty_models:
        raw, adjusted, applied = await penalty_model.apply_penalties(
            responses, uids, penalty_additional_params
        )
        rewards *= applied
        penalty_raw_map[str(penalty_model.name)] = float(raw[0].item())
        penalty_adjusted_map[str(penalty_model.name)] = float(adjusted[0].item())
        penalty_applied_map[str(penalty_model.name)] = float(applied[0].item())
    penalty_stage_elapsed = time.perf_counter() - penalty_stage_start

    final_score = float(rewards[0].item()) if len(rewards) else 0.0
    total_flow_elapsed = time.perf_counter() - flow_start

    # Keep details from summary relevance model for debugging parity.
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

    perf_raw = model_scores.get("performance_score", 0.0)
    perf_penalty = 1.0 - perf_raw
    print(
        "[ai-validation-timing] "
        f"solution_run={elapsed:.3f}s, "
        f"rewards_stage={reward_stage_elapsed:.3f}s, "
        f"penalties_stage={penalty_stage_elapsed:.3f}s, "
        f"final_flow={total_flow_elapsed:.3f}s"
    )
    print(
        "[ai-validation-performance] "
        f"process_time={elapsed:.3f}s, "
        f"max_execution_time={float(getattr(synapse, 'max_execution_time', 0) or 0):.3f}s, "
        f"performance_reward={perf_raw:.6f}, "
        f"performance_penalty={perf_penalty:.6f}"
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
            "summary_preview": synapse.texts.get("summary", "")[:1200],
        },
        "validation": {
            "validator_mode": "DESARCH_PARITY_ADVANCED_SCRAPER",
            "summary_relevance_score": round(summary_score, 6),
            "timing_breakdown_seconds": {
                "solution_run": round(elapsed, 6),
                "rewards_stage": round(reward_stage_elapsed, 6),
                "penalties_stage": round(penalty_stage_elapsed, 6),
                "final_flow_total": round(total_flow_elapsed, 6),
            },
            "reward_model_scores": {k: round(v, 6) for k, v in model_scores.items()},
            "search_content_relevance_zero_diagnostic": search_content_zero_diagnostic,
            "miner_link_scores_preview": {
                k: getattr(v, "value", str(v))
                for k, v in (getattr(synapse, "miner_link_scores", None) or {}).items()
            },
            "reward_weights": {
                "twitter_content_relevance": round(weights[0], 6),
                "search_content_relevance": round(weights[1], 6),
                "summary_relavance_match": round(weights[2], 6),
                "performance_score": round(weights[3], 6),
            },
            "penalties_raw": {k: round(v, 6) for k, v in penalty_raw_map.items()},
            "penalties_adjusted": {
                k: round(v, 6) for k, v in penalty_adjusted_map.items()
            },
            "penalties_applied": {k: round(v, 6) for k, v in penalty_applied_map.items()},
            "final_validation_score": round(final_score, 6),
            "summary_scoring_details": summary_details,
        },
    }


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run one AI task validation.",
        allow_abbrev=False,
    )
    p.add_argument("--task", required=True, help="Task id or filename in tasks/ai.")
    p.add_argument(
        "--twitter-log",
        action="store_true",
        help="Enable detailed AI+Twitter debug logs (raw response previews).",
    )
    return p


def main() -> int:
    parser = _build_parser()
    args, _unknown = parser.parse_known_args()
    report = asyncio.run(_run(args))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
