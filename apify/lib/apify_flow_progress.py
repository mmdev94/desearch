"""Track completed global filter indices k for resume."""

from __future__ import annotations

import json
from pathlib import Path


def load_completed(path: Path) -> set[int]:
    if not path.is_file():
        return set()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    if isinstance(doc, dict):
        raw = doc.get("completed_k") or doc.get("completed")
    else:
        raw = doc
    if not isinstance(raw, list):
        return set()
    return {int(x) for x in raw if str(x).isdigit() or isinstance(x, int)}


def save_completed(path: Path, completed: set[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 1, "completed_k": sorted(completed)}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def mark_done(path: Path, k: int) -> None:
    s = load_completed(path)
    s.add(k)
    save_completed(path, s)
