#!/usr/bin/env python3
"""Generate synthetic DeSearch tasks into ``tasks/*.json``."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from pathlib import Path


def _bootstrap_source_imports() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    source_root = repo_root / "source"
    for p in (repo_root, source_root):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))


def _load_repo_env() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    env_path = repo_root / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        os.environ[key] = value


async def _run(args: argparse.Namespace) -> list[dict]:
    _load_repo_env()
    _bootstrap_source_imports()
    from neurons.validators.scoring.synthetic_query_generator import (
        SyntheticQueryGenerator,
    )

    if args.seed is not None:
        random.seed(args.seed)

    generator = SyntheticQueryGenerator()
    uids = [0]
    verified_by_type = {
        "ai_search": {uid: args.ai for uid in uids},
        "x_search": {uid: args.x for uid in uids},
        "web_search": {uid: args.web for uid in uids},
    }

    generated = await generator.generate_epoch_queries(
        available_uids=uids,
        spread_seconds=0.0,
        delay_start=0.0,
        verified_by_type=verified_by_type,
    )
    out: list[dict] = []
    for item in generated:
        out.append(
            {
                "search_type": item["search_type"],
                "query": item["query"],
            }
        )
    return out


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Generate synthetic ai/x/web tasks from source generator."
    )
    p.add_argument("--ai", type=int, default=1, help="Number of ai_search tasks.")
    p.add_argument("--x", type=int, default=1, help="Number of x_search tasks.")
    p.add_argument("--web", type=int, default=1, help="Number of web_search tasks.")
    p.add_argument("--seed", type=int, default=None)
    return p


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    items = asyncio.run(_run(args))

    tasks_dir = Path(__file__).resolve().parents[1] / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    for idx, item in enumerate(items, start=1):
        path = tasks_dir / f"{idx:04d}_{item['search_type']}.json"
        path.write_text(json.dumps(item, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(items)} task files to {tasks_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
