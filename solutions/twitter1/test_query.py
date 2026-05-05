#!/usr/bin/env python3
"""
Manual test: ``TwitterSearchSynapse``-style stub -> ``solutions.twitter1.query.search``.

Requires env credentials:
  TWITTER_USERNAME=...
  TWITTER_PASSWORD=...
  TWITTER_EMAIL=...

Run from repo root:
  python solutions/twitter1/test_query.py -q "from:MrBeast" --count 10
  python solutions/twitter1/test_query.py -q "from:MrBeast" --count 10 --proxy "http://ip:port"
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

from solutions.twitter1.query import search  # noqa: E402


@dataclass
class _TwitterSearchStub:
    query: str
    sort: str | None = "Latest"
    user: str | None = None
    count: int = 20
    start_date: str | None = None
    end_date: str | None = None
    lang: str | None = None
    verified: bool | None = None
    blue_verified: bool | None = None
    is_quote: bool | None = None
    is_video: bool | None = None
    is_image: bool | None = None
    min_retweets: int | None = None
    min_replies: int | None = None
    min_likes: int | None = None


def main() -> int:
    ap = argparse.ArgumentParser(description="Twikit Twitter search (credential login).")
    ap.add_argument("-q", "--query", required=True)
    ap.add_argument("--user", default=None, help="from: user handle without @")
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--sort", choices=("Latest", "Top"), default="Latest")
    ap.add_argument("--lang", default=None)
    ap.add_argument("--start-date", default=None, dest="start_date")
    ap.add_argument("--end-date", default=None, dest="end_date")
    ap.add_argument("--proxy", default=None, help="HTTP(S) proxy URL, e.g. http://ip:port")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    stub = _TwitterSearchStub(
        query=args.query,
        user=args.user,
        count=args.count,
        sort=args.sort,
        lang=args.lang,
        start_date=args.start_date,
        end_date=args.end_date,
    )

    t0 = time.perf_counter()
    rows: list[dict[str, Any]] = asyncio.run(search(stub, proxy=args.proxy))
    elapsed = time.perf_counter() - t0

    print(f"response_time_s={elapsed:.3f} count={len(rows)}")
    if args.verbose:
        print(json.dumps(rows, indent=2, default=str))
    else:
        for r in rows[:5]:
            print(r.get("id"), (r.get("text") or "")[:120].replace("\n", " "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
