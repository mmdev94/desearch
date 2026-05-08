#!/usr/bin/env python3
"""Manual test for ``solutions.ai.reddit_search`` (Arctic Shift API).

From repo root::

  python solutions/ai/test_reddit.py -q "python asyncio tips"
  python solutions/ai/test_reddit.py -q "discussion" --subreddit MachineLearning
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import importlib.util

_rs_path = Path(__file__).resolve().parent / "reddit_search.py"
# Module name must be registered in sys.modules before exec_module: @dataclass
# resolves sys.modules[cls.__module__] during class creation.
_standalone_name = "solutions_ai_reddit_search_standalone"
_spec = importlib.util.spec_from_file_location(_standalone_name, _rs_path)
reddit_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules[_standalone_name] = reddit_mod
_spec.loader.exec_module(reddit_mod)
RedditQuery = reddit_mod.RedditQuery
arctic_reddit_posts_search = reddit_mod.arctic_reddit_posts_search


def main() -> int:
    ap = argparse.ArgumentParser(description="Arctic Shift Reddit post search.")
    ap.add_argument("-q", "--query", default="", help="Keywords (optional for sub-only browse)")
    ap.add_argument("--max-items", type=int, default=8)
    ap.add_argument(
        "--subreddit",
        default="",
        help="Single subreddit (no r/ prefix needed). If empty, uses defaults + fan-out.",
    )
    ap.add_argument(
        "--sort",
        choices=("asc", "desc"),
        default="desc",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    t0 = time.perf_counter()
    sub = args.subreddit.strip().lstrip("r/") if args.subreddit else None
    rows = asyncio.run(
        arctic_reddit_posts_search(
            RedditQuery(
                query=args.query,
                max_items=args.max_items,
                subreddit=sub,
                sort=args.sort,  # type: ignore[arg-type]
            )
        )
    )
    elapsed = time.perf_counter() - t0

    print(f"response_time_s={elapsed:.3f} count={len(rows)}")
    if args.verbose:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    else:
        for r in rows[: min(10, len(rows))]:
            print(r.get("link"))
            print(f"  {r.get('title', '')[:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
