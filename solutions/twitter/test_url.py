#!/usr/bin/env python3
"""
Manual test: status URLs → ``solutions.twitter.url.search_by_urls``.

Requires ``APIFY_API_KEY``. From repo root:

  python solutions/twitter/test_url.py --url "https://x.com/elonmusk/status/1846987139428634858"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from solutions.twitter.url import search_by_urls  # noqa: E402


@dataclass
class _TwitterURLsStub:
    urls: list[str] = field(default_factory=list)


def main() -> int:
    ap = argparse.ArgumentParser(description="Apify fetch tweets by status URLs (tweetIDs).")
    ap.add_argument(
        "--url",
        action="append",
        dest="urls",
        default=[],
        help="Tweet URL (repeat for multiple)",
    )
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    if not args.urls:
        ap.error("Pass at least one --url")

    t0 = time.perf_counter()
    rows: list[dict[str, Any]] = asyncio.run(
        search_by_urls(_TwitterURLsStub(urls=args.urls))
    )
    elapsed = time.perf_counter() - t0

    print(f"response_time_s={elapsed:.3f} count={len(rows)}")
    if args.verbose:
        print(json.dumps(rows, indent=2, default=str))
    else:
        for r in rows:
            print(r.get("id"), (r.get("text") or "")[:120].replace("\n", " "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
