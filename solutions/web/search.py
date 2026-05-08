"""
Serper.dev Google web search for miners (drop-in for ``WebSearchMiner``).

- Same synapse I/O as ``WebSearchMiner.search``: reads ``query``, ``start``, ``num``;
  fills ``synapse.results`` with ``WebSearchResult`` dicts.
- Credits: stored in Postgres table ``public.serper_api_key`` (columns ``api_key``, ``credits``)
  when ``DATABASE_URL`` is set and the table has at least one row; otherwise falls back to
  ``serper/api-keys.txt`` (``KEY<TAB>credits``). Each successful Serper HTTP response
  decrements the chosen key by 1 (multiple pages cost multiple credits).
- Validates like desearch basics: dedupe by link, then the same
  ``desearch.utils.is_valid_web_search_result`` used in
  ``WebBasicSearchContentRelevanceModel.check_response_random_link`` (second gate after
  duplicate-link detection there). No HTML scrape / ScrapingDog comparison.

``desearch`` is imported **lazily** on first validation (adds ``source/`` to ``sys.path``).
If ``OPENAI_API_KEY`` is unset, a placeholder is set via ``setdefault`` so
``desearch/__init__.py`` can load (Serper web search does not call OpenAI). A real env
key is left unchanged. Add **repo root** to ``sys.path`` for ``from solutions.web…``.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import math
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse, urlunparse

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_ROOT = _REPO_ROOT / "source"

_validator_is_valid_web_search_result: Optional[Callable[..., bool]] = None


def _ensure_source_on_path() -> None:
    if _SOURCE_ROOT.is_dir() and str(_SOURCE_ROOT) not in sys.path:
        sys.path.insert(0, str(_SOURCE_ROOT))


def get_is_valid_web_search_result() -> Callable[..., bool]:
    """Return ``desearch.utils.is_valid_web_search_result`` (validator-aligned, lazy import)."""
    global _validator_is_valid_web_search_result
    if _validator_is_valid_web_search_result is None:
        _ensure_source_on_path()
        # ``desearch/__init__.py`` requires a non-empty key; web search never uses it.
        os.environ.setdefault(
            "OPENAI_API_KEY",
            "unused-placeholder-serper-web-search-only",
        )
        os.environ.setdefault(
            "APIFY_API_KEY",
            "unused-placeholder-serper-web-search-only",
        )
        from desearch.utils import (  # noqa: E402 — lazy to avoid import-time side effects
            is_valid_web_search_result as _fn,
        )

        _validator_is_valid_web_search_result = _fn
    return _validator_is_valid_web_search_result

SERPER_HOST = "google.serper.dev"
SERPER_PATH = "/search"
CREDITS_INITIAL = 2460
DEFAULT_SERPER_PAGE_SIZE = 10
_FILE_HEADER = (
    "# Serper API keys — format: API_KEY<TAB>remaining_credits\n"
    "# Managed by solutions/web/search.py (credits decrease by number of Serper HTTP calls).\n"
)

_serper_storage_lock = threading.Lock()


def _ensure_repo_root_on_path() -> None:
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))


def _load_keys_db() -> list[tuple[str, int]] | None:
    """Load keys from Postgres if ``DATABASE_URL`` is set and ``db.serper`` works."""
    if not (os.environ.get("DATABASE_URL") or "").strip():
        return None
    try:
        _ensure_repo_root_on_path()
        from db.serper import fetch_all_keys  # noqa: E402

        keys = fetch_all_keys()
        return keys if keys else None
    except Exception:
        return None


def _debit_db(api_key: str, debit: int) -> None:
    _ensure_repo_root_on_path()
    from db.serper import debit_credits  # noqa: E402

    debit_credits(api_key, debit)


def _load_keys_for_search(keys_path: Path) -> tuple[list[tuple[str, int]], str]:
    """
    Returns (keys, storage) where storage is ``\"db\"`` or ``\"file\"``.
    Prefer DB when configured and non-empty; otherwise text file.
    """
    db_keys = _load_keys_db()
    if db_keys is not None:
        return db_keys, "db"
    keys = _load_keys(keys_path)
    return keys, "file"


def _normalize_link(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    parts = urlparse(u)
    if parts.scheme.lower() not in ("http", "https"):
        return u.lower()
    path = parts.path.rstrip("/") or "/"
    return urlunparse(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            path,
            parts.params,
            parts.query,
            "",
        )
    )


def _strip_html(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"<[^>]+>", " ", str(text)).strip()


def _serper_post_sync(api_key: str, payload: dict[str, Any], timeout: float = 60.0) -> tuple[int, bytes]:
    conn = http.client.HTTPSConnection(SERPER_HOST, timeout=timeout)
    body = json.dumps(payload)
    headers = {
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
    }
    conn.request("POST", SERPER_PATH, body, headers)
    res = conn.getresponse()
    data = res.read()
    return res.status, data


def _parse_keys_file(raw: str) -> list[tuple[str, int]]:
    keys: list[tuple[str, int]] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if len(parts) >= 2 and parts[-1].isdigit():
            key, credits_s = parts[0], parts[-1]
            credits = int(credits_s)
        else:
            key, credits = parts[0], CREDITS_INITIAL
        if key in seen:
            continue
        seen.add(key)
        keys.append((key, max(0, credits)))
    return keys


def _format_keys_file(keys: list[tuple[str, int]]) -> str:
    lines = [_FILE_HEADER]
    for key, credits in keys:
        lines.append(f"{key}\t{credits}\n")
    return "".join(lines)


def _default_keys_path() -> Path:
    return _REPO_ROOT / "serper" / "api-keys.txt"


def _load_keys(path: Path) -> list[tuple[str, int]]:
    if not path.is_file():
        raise FileNotFoundError(f"Serper API keys file not found: {path}")
    return _parse_keys_file(path.read_text(encoding="utf-8"))


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _pick_key(keys: list[tuple[str, int]]) -> int:
    """Index of key with highest remaining credits (>0)."""
    best_i = -1
    best_c = -1
    for i, (_k, c) in enumerate(keys):
        if c > best_c:
            best_c = c
            best_i = i
    return best_i


def _extract_organic(obj: dict[str, Any]) -> list[dict[str, Any]]:
    org = obj.get("organic")
    if isinstance(org, list):
        return org
    org = obj.get("organic_results")
    if isinstance(org, list):
        return org
    return []


def _raw_item_to_row(item: dict[str, Any]) -> dict[str, Any]:
    title = _strip_html(item.get("title") or "")
    link = (item.get("link") or "").strip()
    snippet = _strip_html(item.get("snippet") or "")
    date = item.get("date")
    row: dict[str, Any] = {
        "title": title,
        "link": link,
        "snippet": snippet,
    }
    if date is not None and str(date).strip():
        row["date"] = str(date).strip()
    return row


def _basic_row_ok(row: dict[str, Any]) -> bool:
    link = row.get("link") or ""
    if not link.startswith(("http://", "https://")):
        return False
    title = (row.get("title") or "").strip()
    if len(title) < 1:
        return False
    if row.get("snippet") is None:
        return False
    return True


def _finalize_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """Shape compatible with ``desearch.protocol.WebSearchResult`` (no pydantic runtime dep)."""
    if not _basic_row_ok(row):
        return None
    title = str(row["title"]).strip()
    link = str(row["link"]).strip()
    snippet = row.get("snippet")
    snippet_s = "" if snippet is None else str(snippet).strip()
    out: dict[str, Any] = {"title": title, "link": link, "snippet": snippet_s}
    if row.get("date") is not None and str(row.get("date")).strip():
        out["date"] = str(row["date"]).strip()
    return out


def _validate_and_dedupe(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """``_finalize_row`` → ``is_valid_web_search_result`` → dedupe by normalized link."""
    is_valid = get_is_valid_web_search_result()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    stats = {
        "raw_count": len(rows),
        "valid_count": 0,
        "invalid_basic_shape": 0,
        "invalid_validator_schema": 0,
        "invalid_duplicate_or_empty_norm_link": 0,
    }
    for row in rows:
        dumped = _finalize_row(row)
        if dumped is None:
            stats["invalid_basic_shape"] += 1
            continue
        if not is_valid(dumped):
            stats["invalid_validator_schema"] += 1
            continue
        norm = _normalize_link(dumped["link"])
        if not norm or norm in seen:
            stats["invalid_duplicate_or_empty_norm_link"] += 1
            continue
        seen.add(norm)
        out.append(dumped)
    stats["valid_count"] = len(out)
    return out, stats


class SerperWebSearch:
    """
    Google search via Serper with per-key credit tracking (Postgres ``serper_api_key`` or
    ``serper/api-keys.txt``).

    Usage (same shape as ``WebSearchMiner``)::

        solution = SerperWebSearch()
        synapse = await solution.search(synapse)
    """

    def __init__(
        self,
        keys_path: Optional[Path] = None,
        *,
        credits_initial: int = CREDITS_INITIAL,
        max_pages_per_query: int = 8,
        request_timeout: float = 60.0,
        fetch_multiplier: float = 1.5,
        page_size: int = DEFAULT_SERPER_PAGE_SIZE,
        max_page_workers: int = 8,
    ) -> None:
        self.keys_path = Path(keys_path) if keys_path else _default_keys_path()
        self.credits_initial = credits_initial
        self.max_pages_per_query = max_pages_per_query
        self.request_timeout = request_timeout
        self.fetch_multiplier = max(1.0, float(fetch_multiplier))
        self.page_size = max(1, min(100, int(page_size)))
        self.max_page_workers = max(1, int(max_page_workers))

    def _save_all_keys(self, keys: list[tuple[str, int]]) -> None:
        content = _format_keys_file(keys)
        _atomic_write(self.keys_path, content)

    def _adjust_credits_for_key(self, api_key: str, keys: list[tuple[str, int]], debit: int) -> None:
        if debit <= 0:
            return
        for i, (k, c) in enumerate(keys):
            if k == api_key:
                keys[i] = (k, max(0, c - debit))
                self._save_all_keys(keys)
                return
        keys.append((api_key, max(0, self.credits_initial - debit)))
        self._save_all_keys(keys)

    async def _fetch_page(
        self,
        api_key: str,
        query: str,
        *,
        num: int,
        page: int,
    ) -> tuple[int, dict[str, Any] | None]:
        payload: dict[str, Any] = {"q": query, "num": min(100, max(1, num)), "page": page}
        status, body = await asyncio.to_thread(
            _serper_post_sync,
            api_key,
            payload,
            self.request_timeout,
        )
        if status != 200:
            return status, None
        try:
            return status, json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return status, None

    async def search(self, synapse: Any) -> Any:
        query = (getattr(synapse, "query", None) or "").strip()
        start = max(0, int(getattr(synapse, "start", 0) or 0))
        num = max(1, int(getattr(synapse, "num", 10) or 10))
        need_end = start + num
        raw_target = max(need_end, int(math.ceil(need_end * self.fetch_multiplier)))

        with _serper_storage_lock:
            keys, storage = _load_keys_for_search(self.keys_path)
            idx = _pick_key(keys)
            if idx < 0 or keys[idx][1] <= 0:
                hint = (
                    "public.serper_api_key (DATABASE_URL)"
                    if storage == "db"
                    else str(self.keys_path)
                )
                raise RuntimeError(
                    "No Serper API key with remaining credits. "
                    f"Add keys or top up credits in {hint}"
                )
            api_key = keys[idx][0]

        pages_needed = max(1, int(math.ceil(raw_target / self.page_size)))
        pages_to_fetch = min(self.max_pages_per_query, pages_needed)
        workers = min(self.max_page_workers, pages_to_fetch)
        page_numbers = list(range(1, pages_to_fetch + 1))

        semaphore = asyncio.Semaphore(workers)

        async def _fetch_one(page: int):
            async with semaphore:
                status, data = await self._fetch_page(
                    api_key,
                    query,
                    num=self.page_size,
                    page=page,
                )
                return page, status, data

        fetched = await asyncio.gather(*[_fetch_one(p) for p in page_numbers])
        fetched.sort(key=lambda x: x[0])  # stable ordering by page number

        raw_rows: list[dict[str, Any]] = []
        http_hits = 0
        pages_with_data = 0
        for _page, status, data in fetched:
            if status != 200 or data is None:
                continue
            http_hits += 1
            organic = _extract_organic(data)
            if not organic:
                continue
            pages_with_data += 1
            for item in organic:
                if isinstance(item, dict):
                    raw_rows.append(_raw_item_to_row(item))

        validated, validation_stats = _validate_and_dedupe(raw_rows)
        sliced = validated[start : start + num]

        synapse.results = sliced

        invalid_count = (
            validation_stats["invalid_basic_shape"]
            + validation_stats["invalid_validator_schema"]
            + validation_stats["invalid_duplicate_or_empty_norm_link"]
        )
        print(
            "[serper] response result count="
            f"{validation_stats['raw_count']} "
            f"valid count={validation_stats['valid_count']} "
            f"invalid count={invalid_count}; "
            "invalid because "
            f"basic_shape={validation_stats['invalid_basic_shape']}, "
            f"validator_schema={validation_stats['invalid_validator_schema']}, "
            "duplicate_or_empty_norm_link="
            f"{validation_stats['invalid_duplicate_or_empty_norm_link']}"
        )
        print(
            "[serper] pages requested="
            f"{pages_to_fetch} workers={workers} page_size={self.page_size} "
            f"pages_with_data={pages_with_data} http_hits={http_hits}"
        )

        if http_hits > 0:
            with _serper_storage_lock:
                if storage == "db":
                    _debit_db(api_key, http_hits)
                else:
                    keys2 = _load_keys(self.keys_path)
                    self._adjust_credits_for_key(api_key, keys2, http_hits)

        return synapse


async def run_web_search(
    synapse: Any,
    *,
    keys_path: Optional[Path] = None,
) -> Any:
    """One-shot helper matching ``WebSearchMiner.search`` signature pattern."""
    return await SerperWebSearch(keys_path=keys_path).search(synapse)


__all__ = [
    "SerperWebSearch",
    "run_web_search",
    "CREDITS_INITIAL",
    "get_is_valid_web_search_result",
]
