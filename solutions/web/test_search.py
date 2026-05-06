#!/usr/bin/env python3
"""
Manual test for ``solutions.web.search.SerperWebSearch``.

Run from repo root (so ``serper/`` resolves; ``source/`` is not required here):

  python solutions/web/test_search.py --query "what is bitcoin" --num 5
  python solutions/web/test_search.py -q "python asyncio" --start 0 --num 10 --verbose

Prints ``response_time_s``: wall-clock for one miner-style pass (synapse in →
Serper.dev HTTP(s) + local validation + ``synapse.results`` filled).

Uses a small local synapse stub with the same fields ``SerperWebSearch`` reads
(``query``, ``start``, ``num``, ``results``). Result rows are filtered with
``desearch.utils.is_valid_web_search_result`` (lazy import on first search — same
function as the validator’s second check in ``check_response_random_link``). If
``OPENAI_API_KEY`` is unset, ``search.py`` sets a placeholder so ``desearch`` can load.
``--dry-run`` does not import ``desearch``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from solutions.web.search import (  # noqa: E402
    CREDITS_INITIAL,
    DEFAULT_SERPER_PAGE_SIZE,
    SerperWebSearch,
    _default_keys_path,
    get_is_valid_web_search_result,
)


@dataclass
class _WebSearchSynapseStub:
    """Same surface as ``desearch.protocol.WebSearchSynapse`` for ``SerperWebSearch.search``."""

    query: str = ""
    start: int = 0
    num: int = 10
    max_execution_time: int | None = None
    results: List[dict[str, Any]] = field(default_factory=list)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run one Serper-backed web search (same params as WebSearchSynapse)."
    )
    ap.add_argument("-q", "--query", default="apple inc", help="Search query")
    ap.add_argument(
        "--num",
        type=int,
        default=10,
        help="Max results to return after start (WebSearchSynapse.num, default 10)",
    )
    ap.add_argument(
        "--start",
        type=int,
        default=0,
        help="Skip first N validated results (WebSearchSynapse.start, default 0)",
    )
    ap.add_argument(
        "--max-execution-time",
        type=int,
        default=None,
        help=(
            "WebSearchSynapse.max_execution_time (validator default 10s). "
            "Default for this test: 10 if omitted — used only for printed comparison vs response_time_s."
        ),
    )
    ap.add_argument(
        "--keys-file",
        type=Path,
        default=None,
        help=f"Path to api-keys.txt (default: {_default_keys_path()})",
    )
    ap.add_argument(
        "--max-pages",
        type=int,
        default=8,
        help="Max Serper pagination requests per miner search (default 8)",
    )
    ap.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_SERPER_PAGE_SIZE,
        help="Serper results requested per page call (default 10)",
    )
    ap.add_argument(
        "--max-page-workers",
        type=int,
        default=8,
        help="Max concurrent page fetch workers (default 8)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print params and exit without calling Serper or mutating api-keys.txt",
    )
    ap.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print full results JSON and keys file path",
    )
    args = ap.parse_args()

    keys_path = args.keys_file.resolve() if args.keys_file else _default_keys_path()

    if args.verbose:
        print(f"repo_root={_REPO}")
        print(f"keys_path={keys_path}")
        print(f"CREDITS_INITIAL={CREDITS_INITIAL}")

    if args.dry_run:
        max_exec_dry = 10 if args.max_execution_time is None else args.max_execution_time
        print(
            json.dumps(
                {
                    "query": args.query,
                    "start": args.start,
                    "num": args.num,
                    "max_execution_time": max_exec_dry,
                    "keys_path": str(keys_path),
                    "max_pages": args.max_pages,
                    "page_size": args.page_size,
                    "max_page_workers": args.max_page_workers,
                },
                indent=2,
            )
        )
        return 0

    max_exec = 10 if args.max_execution_time is None else args.max_execution_time
    synapse = _WebSearchSynapseStub(
        query=args.query.strip(),
        start=args.start,
        num=args.num,
        max_execution_time=max_exec,
    )

    async def _run() -> None:
        client = SerperWebSearch(
            keys_path=keys_path,
            max_pages_per_query=args.max_pages,
            page_size=args.page_size,
            max_page_workers=args.max_page_workers,
        )
        await client.search(synapse)

    # Wall-clock for the same work a miner does inside web_search(): Serper + validate + fill synapse.
    t0 = time.perf_counter()
    asyncio.run(_run())
    response_time_s = time.perf_counter() - t0

    results = synapse.results or []
    print(
        f"response_time_s={response_time_s:.3f}  "
        f"(synapse → Serper.dev → WebSearchSynapse.results; "
        f"excludes axon/dendrite overhead)"
    )
    print(
        f"max_execution_time_s={synapse.max_execution_time}  "
        f"within_synapse_budget={response_time_s <= float(synapse.max_execution_time)}"
    )
    # Validator WebScraperValidator: target_time=2.0, min_realistic_time=0.7 for performance slice.
    print(
        f"validator_perf_target_s=2.0  "
        f"full_perf_credit_band={0.7 <= response_time_s <= 2.0}"
    )
    print(f"results_count={len(results)} (requested num={args.num}, start={args.start})")

    is_valid = get_is_valid_web_search_result()
    valid_flags = [is_valid(r) for r in results]
    print(
        f"is_valid_web_search_result_all={all(valid_flags)} "
        f"(desearch.utils.is_valid_web_search_result, validator 2nd check)"
    )
    if args.verbose and results and not all(valid_flags):
        for i, ok in enumerate(valid_flags):
            if not ok:
                print(f"  fail_index={i} row={json.dumps(results[i], ensure_ascii=False)}")

    if args.verbose:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        for i, row in enumerate(results):
            title = row.get("title", "")[:80]
            link = row.get("link", "")
            print(f"{i + 1}. {title!r}\n   {link}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
