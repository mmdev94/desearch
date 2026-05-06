#!/usr/bin/env python3
"""
Manual test for ``solutions.ai.youtube_search_pkg`` (youtube-search on PyPI).

Run from repo root:
  python solutions/ai/test_youtube.py -q "python asyncio tutorial" --max-items 8
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

from solutions.ai.youtube_search_pkg import YoutubeQuery, youtube_search  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="YouTube search via youtube-search package.")
    ap.add_argument("-q", "--query", required=True)
    ap.add_argument("--max-items", type=int, default=10)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    t0 = time.perf_counter()
    rows = asyncio.run(
        youtube_search(YoutubeQuery(query=args.query, max_items=args.max_items))
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
