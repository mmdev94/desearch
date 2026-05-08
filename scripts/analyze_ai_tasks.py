#!/usr/bin/env python3
"""
Analyze ``tasks/ai/*.json`` with GPT-4.1-nano (same budget model as RewardLLM / dataset helpers).

Produces **per-tool search rules** aligned with subnet AI-search validation:

- ``TwitterContentRelevanceModel``: validator re-fetches sampled tweets and scores tweet
  body vs the miner ``prompt`` using ``LinkContentPrompt`` (0–10 relevance).
- ``WebSearchContentRelevanceModel``: random link(s) from each tool group's search results
  are scraped; scraped text vs ``prompt`` is scored with ``SearchSummaryRelevancePrompt``
  (discrete relevance scale centred on keywords/themes depth in the scraped page).
  URLs must belong to miner-returned lists (subset domain attribution for Reddit / Wikipedia /
  YouTube / arXiv / HN vs generic web).
- ``SummaryRelevanceRewardModel``: final answer must use ``**`` section headers,
  cite ≥3 Markdown links whose URLs normalize into miner ``miner_tweets`` or any
  ``*_search_results`` ``link`` field; verification ratio must be ≥ 50%.

The script merges an ``ai_search_analysis`` object into each JSON task file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ANALYSIS_MODEL = "gpt-4.1-nano"
_ANALYSIS_SCHEMA_VERSION = "1.0"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _bootstrap_source_imports() -> None:
    repo_root = _repo_root()
    source_root = repo_root / "source"
    for p in (repo_root, source_root):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))


def load_dotenv_manual() -> None:
    import os

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


def _tasks_ai_dir() -> Path:
    d = _repo_root() / "tasks" / "ai"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _resolve_task_paths(args: argparse.Namespace) -> list[Path]:
    d = _tasks_ai_dir()
    if args.all:
        return sorted(d.glob("*.json"))
    out: list[Path] = []
    for tid in args.task:
        stem = tid
        if tid.isdigit():
            stem = f"{int(tid):04d}"
        p = d / (stem if stem.endswith(".json") else f"{Path(stem).stem}.json")
        out.append(p)
    return out


def _strip_json_fence(text: str) -> str:
    t = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)```\s*$", t, re.DOTALL | re.IGNORECASE)
    if fence:
        return fence.group(1).strip()
    return t


def _parse_analysis_json(raw: str) -> dict[str, Any]:
    t = _strip_json_fence(raw)
    return json.loads(t)


SYSTEM_VALIDATION_DIGEST = """You are a strict analyst for DESearch miners (Bittensor AI search).

Validators combine these signals (subnet code intent):

T — Twitter relevance: validator re-scrapes sampled tweets and scores tweet text vs the user prompt with `LinkContentPrompt` (0–10; needs keyword/theme coverage in the tweet body).

W — Web-style search tools (Web / Wikipedia / YouTube / ArXiv / Reddit / Hacker News): random URLs from the miner’s matching `*_search_results` lists are scraped; scraped text is scored vs the prompt with `SearchSummaryRelevancePrompt` (2/5/9 style: missing vs shallow vs deep engagement with question keywords).

Link attribution: those URLs must already appear in the miner’s returned result lists (domain-specific lists for Reddit, Wikipedia, YouTube, arXiv, HN).

S — Final summary: markdown must use `**` section headers, include ≥3 `[text](url)` citations, and ≥50% of cited URLs must normalize-match returned tweet permalinks or search-result `link` values.

Mining goal: every tool should retrieve items whose visible text can support the same core answer so link-scraping and final-summary verification stay consistent."""

USER_INSTRUCTION_TEMPLATE = """Task JSON excerpt (miner will receive):

{task_excerpt}

Return ONE JSON object (no prose outside JSON) with this shape:

{{
  "schema_version": "{schema_version}",
  "task_classification": {{
    "format": "<one of: framed_question | open_question | analytic_brief | news_followup>",
    "summary": "<one sentence what the miner must argue or explain>",
    "is_full_question_prompt": "<true|false — true if prose is ONLY a finished question>",
    "core_information_need": "<the minimal answer target in one clause>"
  }},
  "main_content_hypothesis": "<short phrase miners should see reflected verbatim or paraphrased strongly in each tool's retrieved items>",
  "canonical_keywords_for_validation": ["<6–12 thematic tokens/phrases validators look for across scraped snippets>"],
  "entities": ["<proper nouns if any — country, bill, tech, metric>"],
  "cross_tool_notes": "<how SAME themes must show on Twitter vs web-specialty sources given date_filter>",
  "tool_search_rules": {{
    "Twitter Search": {{
      "search_query_candidates": ["<short noisy Twitter query variants>"],
      "must_cover_in_snapshot_text": ["<terms that SHOULD appear inside returned tweet bodies>"],
      "avoid_ambiguity_notes": "<optional>",
      "date_filter_hints": "<how task date_filter affects recency>"
    }},
    "... only include ONE key per ACTUAL entry in \\\"tools\\\", keyed by IDENTICAL spelling (e.g., \\\"Reddit Search\\\", \\\"Hacker News Search\\\") ..."
  }},
  "summary_citation_plan": {{
    "suggested_outline_headers": ["<** header ideas** aligned to prompt>"],
    "link_budget": {{
      "minimum_distinct_domains": "<integer ideally ≥ number of activated tool-groups>",
      "notes": "<which tool each cited URL shape should originate from>"
    }}
  }},
  "risk_flags": ["<where miners might mismatch validation e.g., vague geopolitics, fast-moving stocks>"]
}}

Rules:
1. Populate ``tool_search_rules`` ONLY for tool strings present in JSON ``tools`` (exact wording).
2. Reddit Search: retrieval APIs often NEED ``subreddit`` (or ``author``) paired with keywords—suggest r/subreddit + query text in tool rules.
3. Hacker News: prefer searchable story-query strings; cite whether ``show_hn`` / ``ask_hn`` tagging might help when relevant.
4. ArXiv / Wikipedia / YouTube / Web Search: tailor query phrasing per platform norms.
5. If the task text is ONLY a polished question vs a long briefing, set ``is_full_question_prompt`` appropriately and derive ``canonical_keywords_for_validation`` from the core information need shared across ALL tools listed.
6. Output valid JSON ONLY (ASCII double quotes, no trailing commentary)."""


async def analyze_one_task(payload: dict[str, Any], model: str) -> dict[str, Any]:
    from desearch.utils import call_openai

    qblock = payload.get("query") or {}
    excerpt = json.dumps(
        {
            "search_type": payload.get("search_type"),
            "query": qblock,
        },
        indent=2,
        ensure_ascii=False,
    )

    messages = [
        {"role": "system", "content": SYSTEM_VALIDATION_DIGEST},
        {
            "role": "user",
            "content": USER_INSTRUCTION_TEMPLATE.format(
                task_excerpt=excerpt,
                schema_version=_ANALYSIS_SCHEMA_VERSION,
            ),
        },
    ]

    raw = await call_openai(messages, model=model, temperature=0.2)
    if not raw:
        raise RuntimeError("OpenAI returned empty response (check OPENAI_API_KEY).")

    try:
        return _parse_analysis_json(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse model JSON: {e}\n---\n{raw}\n---") from e


def merge_analysis(
    task: dict[str, Any], analysis_body: dict[str, Any], model: str
) -> dict[str, Any]:
    tools_list = []
    qp = task.get("query") or {}
    tools_list = list(qp.get("tools") or [])

    out = dict(task)
    ai_block = {
        "model": model,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **analysis_body,
    }

    predicted = list((analysis_body.get("tool_search_rules") or {}).keys())
    missing = sorted(set(tools_list) - set(predicted))
    extras = sorted(set(predicted) - set(tools_list))

    ai_block["_consistency_hints"] = {
        "tools_in_task": tools_list,
        "tools_analyzed_keys": predicted,
        "missing_tool_keys_vs_task": missing,
        "extra_keys_not_in_task": extras,
        "analysis_schema_version": _ANALYSIS_SCHEMA_VERSION,
    }

    out["ai_search_analysis"] = ai_block
    return out


async def main_async(args: argparse.Namespace) -> int:
    load_dotenv_manual()
    _bootstrap_source_imports()

    paths = _resolve_task_paths(args)
    if not paths:
        print("No task files.")
        return 1

    model = args.model.strip() or ANALYSIS_MODEL

    for path in paths:
        if not path.is_file():
            print(f"[skip missing] {path}", file=sys.stderr)
            continue
        raw_text = path.read_text(encoding="utf-8")
        payload = json.loads(raw_text)

        if payload.get("search_type") != "ai_search":
            print(f"[skip wrong type] {path.name}: {payload.get('search_type')}")
            continue

        if (
            payload.get("ai_search_analysis")
            and not args.force
            and not args.dry_run
        ):
            print(f"[skip existing analysis] {path.name} (--force to overwrite)")
            continue

        try:
            body = await analyze_one_task(payload, model=model)
        except Exception as e:
            print(f"[error] {path.name}: {e}", file=sys.stderr)
            if args.fail_fast:
                return 2
            continue

        merged = merge_analysis(payload, body, model=model)

        if args.dry_run:
            hints = merged.get("ai_search_analysis", {}).get("_consistency_hints", {})
            miss = hints.get("missing_tool_keys_vs_task") or []
            if miss:
                print(
                    f"[warn] {path.name} model omitted tool keys: {miss}",
                    file=sys.stderr,
                )
            print(f"=== {path.name} ===")
            print(json.dumps(merged.get("ai_search_analysis"), indent=2)[:6000])
            print("… dry-run truncation …")
        else:
            miss = (
                merged.get("ai_search_analysis", {})
                .get("_consistency_hints", {})
                .get("missing_tool_keys_vs_task")
                or []
            )
            if miss:
                print(
                    f"[warn] {path.name} model omitted tool keys: {miss}",
                    file=sys.stderr,
                )
            path.write_text(
                json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            print(f"[wrote] {path.name}")

    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Analyze AI-search tasks → tool-specific search strategies for validation."
    )
    p.add_argument(
        "--task",
        nargs="*",
        default=[],
        help="Task stem(s): 1 maps to tasks/ai/0001.json (default empty if --all).",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="Analyze every tasks/ai/*.json",
    )
    p.add_argument(
        "--model",
        default=ANALYSIS_MODEL,
        help=f"Chat model default {ANALYSIS_MODEL}",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print analysis JSON only; don't write.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing ai_search_analysis block.",
    )
    p.add_argument(
        "--fail-fast",
        action="store_true",
        help="Abort on first OpenAI/analysis error.",
    )
    return p


def main() -> None:
    ap = build_arg_parser()
    args = ap.parse_args()
    if not args.all and not args.task:
        ap.error("Provide --all or one or more --task ids")

    code = asyncio.run(main_async(args))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
