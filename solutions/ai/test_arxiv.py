#!/usr/bin/env python3
"""
Manual test for ``solutions.ai.arxiv_search`` (arxiv PyPI package).

Run from repo root:
  python solutions/ai/test_arxiv.py -q "llm agents" --max-items 10
  python solutions/ai/test_arxiv.py --id-list "1605.08386v1,2401.00001" --verbose
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

from solutions.ai.arxiv_search import ArxivQuery, arxiv_search  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="ArXiv search via arxiv.py.")
    ap.add_argument("-q", "--query", default="", help="Search query")
    ap.add_argument("--max-items", type=int, default=10)
    ap.add_argument(
        "--sort-by",
        choices=("relevance", "submitted_date", "last_updated_date"),
        default="relevance",
    )
    ap.add_argument(
        "--sort-order",
        choices=("ascending", "descending"),
        default="descending",
    )
    ap.add_argument(
        "--id-list",
        default="",
        help="Comma-separated arXiv IDs (optional)",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    ids = [x.strip() for x in args.id_list.split(",") if x.strip()] if args.id_list else None
    if not args.query.strip() and not ids:
        ap.error("Provide --query or --id-list")

    t0 = time.perf_counter()
    rows = asyncio.run(
        arxiv_search(
            ArxivQuery(
                query=args.query,
                max_items=args.max_items,
                sort_by=args.sort_by,
                sort_order=args.sort_order,
                id_list=ids,
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

