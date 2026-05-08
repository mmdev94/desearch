#!/usr/bin/env python3
"""
Twex signup automation:
- HTTP proxies from ``twex/proxies.txt`` via **selenium-wire** (undetected Chrome → wire → your proxy)
- temp Outlook via Smailpro (SONJJ_* env)
- verify, dashboard API key → ``public.twex_account``

Runs **until Ctrl+C** by default. Use ``--count N`` to stop after N attempts.

Requires ``selenium-wire`` (see ``pyproject.toml``); ``blinker`` is pinned for compatibility with selenium-wire's bundled mitmproxy.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--count",
        type=int,
        default=0,
        metavar="N",
        help="Stop after N signup attempts (0 = infinite until Ctrl+C). Default: 0.",
    )
    ap.add_argument(
        "--proxies-file",
        type=Path,
        default=None,
        help=f"HTTP proxies list (default: {_SCRIPT_DIR / 'proxies.txt'}).",
    )
    args = ap.parse_args()

    from db.pg import load_env  # noqa: PLC0415
    from twex.lib.auto_signup import run_twex_signup_flow  # noqa: PLC0415

    load_env()
    count = None if int(args.count) <= 0 else int(args.count)
    proxies_path = args.proxies_file if args.proxies_file is not None else _SCRIPT_DIR / "proxies.txt"
    run_twex_signup_flow(count=count, proxies_file=proxies_path)


if __name__ == "__main__":
    main()
