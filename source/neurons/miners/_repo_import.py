"""Ensure repo root is on ``sys.path`` so ``solutions.*`` and ``db.*`` resolve."""

from __future__ import annotations

import sys
from pathlib import Path


def ensure_repo_root_on_path() -> Path:
    """``neurons/miners/*.py`` → parents[3] is the repository root (contains ``solutions/``)."""
    root = Path(__file__).resolve().parents[3]
    r = str(root)
    if r not in sys.path:
        sys.path.insert(0, r)
    return root
