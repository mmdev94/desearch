"""Per-validator, per-search-type concurrency (manifest limits = slots per dendrite hotkey)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Callable

from desearch.miner_config import MAX_CONCURRENCY_PER_TYPE, MinerManifest

# Synapse family keys aligned with ``miner_config.SEARCH_TYPES`` / manifest JSON.
AI_SEARCH_KIND = "ai_search"
WEB_SEARCH_KIND = "web_search"
X_SEARCH_KIND = "x_search"


class PerValidatorConcurrencyGate:
    """
    One asyncio semaphore per (kind, validator_hotkey), capacity = manifest limit for that kind.

    Documentation treats manifest concurrency as per-validator; this enforces that by giving each
    dendrite hotkey its own bucket of parallel workers per search type.
    """

    def __init__(self, get_manifest: Callable[[], MinerManifest]):
        self._get_manifest = get_manifest
        self._semaphores: dict[tuple[str, str], asyncio.Semaphore] = {}
        self._limits: dict[tuple[str, str], int] = {}

    def _limit_for_kind(self, kind: str) -> int:
        m = self._get_manifest()
        c = m.concurrency
        if kind == AI_SEARCH_KIND:
            v = c.ai_search
        elif kind == WEB_SEARCH_KIND:
            v = c.web_search
        elif kind == X_SEARCH_KIND:
            v = c.x_search
        else:
            v = 1
        return max(1, min(MAX_CONCURRENCY_PER_TYPE, int(v)))

    def _sem(self, kind: str, hotkey: str | None) -> asyncio.Semaphore:
        hk = (hotkey or "").strip() or "__unknown__"
        key = (kind, hk)
        lim = self._limit_for_kind(kind)
        if key not in self._semaphores or self._limits.get(key) != lim:
            self._semaphores[key] = asyncio.Semaphore(lim)
            self._limits[key] = lim
        return self._semaphores[key]

    @asynccontextmanager
    async def acquire(self, kind: str, hotkey: str | None):
        sem = self._sem(kind, hotkey)
        await sem.acquire()
        try:
            yield
        finally:
            sem.release()
