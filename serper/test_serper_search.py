#!/usr/bin/env python3
"""
Smoke-test Serper Google Search API (https://google.serper.dev/search).

Reads API key from ``api-keys.txt`` in this directory (first non-empty line).
Usage:
  python apify/serper/test_serper_search.py
  python apify/serper/test_serper_search.py --q "python asyncio tutorial"
"""

from __future__ import annotations

import argparse
import http.client
import json
import sys
from pathlib import Path

_SERPER_DIR = Path(__file__).resolve().parent
_DEFAULT_KEYS = _SERPER_DIR / "api-keys.txt"


def _load_api_key(keys_path: Path) -> str:
    if not keys_path.is_file():
        raise SystemExit(f"Missing API keys file: {keys_path}")
    for line in keys_path.read_text(encoding="utf-8").splitlines():
        key = line.strip()
        if key and not key.startswith("#"):
            return key
    raise SystemExit(f"No API key found in {keys_path}")


def run_search(*, query: str, api_key: str) -> tuple[int, str]:
    conn = http.client.HTTPSConnection("google.serper.dev", timeout=60)
    payload = json.dumps({"q": query})
    headers = {
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
    }
    conn.request("POST", "/search", payload, headers)
    res = conn.getresponse()
    body = res.read().decode("utf-8")
    return res.status, body


def main() -> int:
    ap = argparse.ArgumentParser(description="Test Serper Google Search API")
    ap.add_argument(
        "--q",
        "--query",
        dest="query",
        default="apple inc",
        help='Search query (default: "apple inc")',
    )
    ap.add_argument(
        "--keys-file",
        type=Path,
        default=_DEFAULT_KEYS,
        help=f"Path to one-line API key file (default: {_DEFAULT_KEYS.name})",
    )
    args = ap.parse_args()

    api_key = _load_api_key(args.keys_file.resolve())
    status, raw = run_search(query=args.query.strip(), api_key=api_key)

    print(f"HTTP {status}")
    if status != 200:
        print(raw)
        return 1

    try:
        obj = json.loads(raw)
        print(json.dumps(obj, indent=2, ensure_ascii=False))
    except json.JSONDecodeError:
        print(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
