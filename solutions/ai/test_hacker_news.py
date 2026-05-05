#!/usr/bin/env python3
"""
Manual test for ``solutions.ai.hacker_news`` (Algolia HN API).

Run from repo root:
  python solutions/ai/test_hacker_news.py -q "bittensor" --max-items 10
  python solutions/ai/test_hacker_news.py -q "openai" --mode search_by_date --tags story,show_hn --verbose
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from solutions.ai.hacker_news import hn_algolia_search, HackerNewsQuery  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Hacker News search via hn.algolia.com API.")
    ap.add_argument("-q", "--query", required=True, help="Search query")
    ap.add_argument("--max-items", type=int, default=10)
    ap.add_argument("--mode", choices=("search", "search_by_date"), default="search")
    ap.add_argument(
        "--tags",
        default=None,
        help="Comma-separated tags, e.g. story,show_hn (optional)",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    tags = None
    if args.tags:
        tags = [t.strip() for t in args.tags.split(",") if t.strip()]

    t0 = time.perf_counter()
    rows = __import__("asyncio").run(
        hn_algolia_search(
            HackerNewsQuery(
                query=args.query,
                max_items=args.max_items,
                mode=args.mode,
                tags=tags,
            )
        )
    )
    elapsed = time.perf_counter() - t0

    print(f"response_time_s={elapsed:.3f} count={len(rows)}")
    if args.verbose:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    else:
        for r in rows[: min(5, len(rows))]:
            print(r.get("title"), r.get("link"))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

