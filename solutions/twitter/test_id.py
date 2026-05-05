#!/usr/bin/env python3
"""
Manual test: tweet id → ``solutions.twitter.id.search_by_id``.

Requires ``APIFY_API_KEY``. From repo root:

  python solutions/twitter/test_id.py --id 1846987139428634858
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from solutions.twitter.id import search_by_id  # noqa: E402


@dataclass
class _TwitterIDStub:
    id: str


def main() -> int:
    ap = argparse.ArgumentParser(description="Apify fetch tweet by id (new actor tweetIDs).")
    ap.add_argument("--id", required=True, help="Numeric tweet id")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    t0 = time.perf_counter()
    rows: list[dict[str, Any]] = asyncio.run(search_by_id(_TwitterIDStub(id=args.id)))
    elapsed = time.perf_counter() - t0

    print(f"response_time_s={elapsed:.3f} count={len(rows)}")
    if args.verbose:
        print(json.dumps(rows, indent=2, default=str))
    elif rows:
        r = rows[0]
        print(r.get("id"), (r.get("text") or "")[:200].replace("\n", " "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
