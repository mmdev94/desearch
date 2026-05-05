#!/usr/bin/env python3
"""
Manual test for ``solutions.ai.wikipedia_api_search`` (wikipedia-api).

Optional: set ``WIKIPEDIA_USER_AGENT`` to a contact-rich string per Wikimedia policy.

Run from repo root:
  python solutions/ai/test_wikipedia.py -q "Python programming language" --max-items 5
  python solutions/ai/test_wikipedia.py -q "Bittensor" --lang en -v
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

from solutions.ai.wikipedia_api_search import (  # noqa: E402
    WikipediaQuery,
    wikipedia_search,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Wikipedia search via wikipedia-api.")
    ap.add_argument("-q", "--query", required=True, help="Search query")
    ap.add_argument("--max-items", type=int, default=10)
    ap.add_argument("--lang", default="en", help="Wikipedia language code, e.g. en, de")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    t0 = time.perf_counter()
    rows = asyncio.run(
        wikipedia_search(
            WikipediaQuery(
                query=args.query,
                max_items=args.max_items,
                language=args.lang,
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
