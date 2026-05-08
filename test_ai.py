#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from solutions.ai.ai import build_ai_synapse_from_task, run_ai_solution


def _task_path(task: str) -> Path:
    p = Path("tasks/ai")
    if task.isdigit():
        return p / f"{int(task):04d}.json"
    return p / task


async def _run(task_file: Path, show: int) -> dict:
    task = json.loads(task_file.read_text(encoding="utf-8"))
    syn = build_ai_synapse_from_task(task)
    out = await run_ai_solution(syn, task_meta=task)
    summary = out.texts.get("final_summary", "")
    return {
        "task": str(task_file),
        "query": out.prompt,
        "tools": out.tools,
        "counts": {
            "twitter": len(out.miner_tweets or []),
            "web": len(out.search_results or []),
            "reddit": len(out.reddit_search_results or []),
            "youtube": len(out.youtube_search_results or []),
            "arxiv": len(out.arxiv_search_results or []),
            "hackernews": len(out.hacker_news_search_results or []),
            "wikipedia": len(out.wikipedia_search_results or []),
        },
        "summary_preview": summary[:1200],
        "sample_sources": {
            "twitter": (out.miner_tweets or [])[:show],
            "web": (out.search_results or [])[:show],
            "reddit": (out.reddit_search_results or [])[:show],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run AI solution quickly")
    parser.add_argument("--task", default="1", help="Task id or filename in tasks/ai")
    parser.add_argument("--show", type=int, default=2, help="Sample rows per source type")
    args = parser.parse_args()

    task_file = _task_path(args.task)
    if not task_file.is_file():
        raise FileNotFoundError(f"Task file not found: {task_file}")

    report = asyncio.run(_run(task_file, args.show))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
