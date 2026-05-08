"""
ArXiv-flavored paper search via OpenAlex **works** search + source filter for arXiv.

Uses ``filter=primary_location.source.id:S4306400194`` (OpenAlex source id for arXiv).
See `<https://developers.openalex.org/guides/searching>`_.

Populates ``ScraperStreamingSynapse.arxiv_search_results`` with
``SearchResultItem`` dicts ``{title, link, snippet}`` where ``link`` is always on
``arxiv.org`` (``https://arxiv.org/abs/...``) so ``search_content_relevance``
domain attribution matches ``arxiv_search_results``.

Do **not** name this module ``arxiv.py``.

API keys: plain-text lines in ``arxiv-api.txt`` (same directory as this module).
Per-key usage is tracked in ``arxiv-api-quota.txt`` (``<key> <remaining> <YYYY-MM-DD>`` UTC):
1000 calls/day per key; **each successful OpenAlex HTTP response** decrements by 1.
At UTC midnight the remaining count resets to 1000 for that key.
"""

from __future__ import annotations

import asyncio
import random
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import quote, urlparse

import aiohttp

OPENALEX_WORKS = "https://api.openalex.org/works"
ARXIV_SOURCE_FILTER = "primary_location.source.id:S4306400194"
_DEFAULT_DAILY_QUOTA = 1000

_SORT_OPENALEX = {
    "relevance": None,
    "submitted_date": "publication_date:desc",
    "last_updated_date": "updated_date:desc",
}

_MODULE_DIR = Path(__file__).resolve().parent
_KEYS_FILE = _MODULE_DIR / "arxiv-api.txt"
_QUOTA_FILE = _MODULE_DIR / "arxiv-api-quota.txt"

_quota_lock = threading.Lock()


SortBy = Literal["relevance", "submitted_date", "last_updated_date"]
SortOrder = Literal["ascending", "descending"]


@dataclass(frozen=True)
class ArxivQuery:
    query: str
    max_items: int = 10
    sort_by: SortBy = "relevance"
    sort_order: SortOrder = "descending"
    id_list: Optional[list[str]] = None


def _utc_date_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _load_api_keys() -> list[str]:
    if not _KEYS_FILE.is_file():
        return []
    keys: list[str] = []
    for raw in _KEYS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        keys.append(parts[0].strip())
    return keys


def _load_quota_map() -> dict[str, tuple[int, str]]:
    """key -> (remaining, utc_date_str)."""
    out: dict[str, tuple[int, str]] = {}
    if not _QUOTA_FILE.is_file():
        return out
    for raw in _QUOTA_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        key, rem_s, day = parts[0], parts[1], parts[2]
        try:
            out[key] = (int(rem_s), day)
        except ValueError:
            continue
    return out


def _write_quota_map(keys_in_order: list[str], state: dict[str, tuple[int, str]]) -> None:
    today = _utc_date_str()
    lines = []
    for k in sorted(keys_in_order):
        rem, day = state.get(k, (_DEFAULT_DAILY_QUOTA, today))
        if day != today:
            rem, day = _DEFAULT_DAILY_QUOTA, today
        lines.append(f"{k} {rem} {day}\n")
    _QUOTA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _QUOTA_FILE.with_suffix(".tmp")
    tmp.write_text("# OpenAlex API usage (UTC day). Auto-maintained.\n" + "".join(lines), encoding="utf-8")
    tmp.replace(_QUOTA_FILE)


def _merge_key_state(api_keys: list[str]) -> dict[str, tuple[int, str]]:
    today = _utc_date_str()
    disk = _load_quota_map()
    state: dict[str, tuple[int, str]] = {}
    for k in api_keys:
        if k in disk:
            rem, day = disk[k]
            if day != today:
                state[k] = (_DEFAULT_DAILY_QUOTA, today)
            else:
                state[k] = (rem, day)
        else:
            state[k] = (_DEFAULT_DAILY_QUOTA, today)
    return state


def _remaining_for_key(state: dict[str, tuple[int, str]], key: str) -> int:
    today = _utc_date_str()
    rem, day = state.get(key, (_DEFAULT_DAILY_QUOTA, today))
    if day != today:
        return _DEFAULT_DAILY_QUOTA
    return rem


def _apply_quota_decrement(key: str, api_keys: list[str]) -> None:
    today = _utc_date_str()
    with _quota_lock:
        state = _merge_key_state(api_keys)
        rem, day = state.get(key, (_DEFAULT_DAILY_QUOTA, today))
        if day != today:
            rem, day = _DEFAULT_DAILY_QUOTA, today
        rem = max(0, rem - 1)
        state[key] = (rem, today)
        _write_quota_map(api_keys, state)


def _reconstruct_abstract(inv_index: Optional[dict[str, list[int]]]) -> str:
    if not inv_index:
        return ""
    chunks: list[tuple[int, str]] = []
    for word, positions in inv_index.items():
        if not isinstance(positions, list):
            continue
        for pos in positions:
            if isinstance(pos, int):
                chunks.append((pos, word))
    chunks.sort(key=lambda x: x[0])
    return " ".join(w for _, w in chunks).strip()


_ARXIV_DOI_ID_RE = re.compile(
    r"arxiv[\./](?P<id>\d{4}\.\d{4,5}(?:v\d+)?)", re.IGNORECASE
)
_ARXIV_PATH_RE = re.compile(
    r"/(?:abs|pdf)/(?P<id>[\w.-]+)(?:\.pdf)?/?$", re.IGNORECASE
)


def _url_to_arxiv_abs(url: str) -> Optional[str]:
    if not url or not isinstance(url, str):
        return None
    p = urlparse(url.strip())
    host = (p.netloc or "").lower()
    path = p.path or ""

    if host.endswith("arxiv.org"):
        m = _ARXIV_PATH_RE.search(path)
        if m:
            return f"https://arxiv.org/abs/{m.group('id')}"

    if "doi.org" in host:
        blob = f"{host}{path}".lower().replace("/", ".")
        m = _ARXIV_DOI_ID_RE.search(blob)
        if m:
            return f"https://arxiv.org/abs/{m.group('id')}"

    blob_all = url.lower()
    m = _ARXIV_DOI_ID_RE.search(blob_all.replace("/", "."))
    if m:
        return f"https://arxiv.org/abs/{m.group('id')}"

    return None


def _canonical_arxiv_abs_url(work: dict[str, Any]) -> Optional[str]:
    urls: list[str] = []
    pl = work.get("primary_location")
    if isinstance(pl, dict):
        for fld in ("landing_page_url", "pdf_url"):
            v = pl.get(fld)
            if v:
                urls.append(str(v))

    for loc in work.get("locations") or []:
        if not isinstance(loc, dict):
            continue
        for fld in ("landing_page_url", "pdf_url"):
            v = loc.get(fld)
            if v:
                urls.append(str(v))

    bol = work.get("best_oa_location")
    if isinstance(bol, dict):
        for fld in ("landing_page_url", "pdf_url"):
            v = bol.get(fld)
            if v:
                urls.append(str(v))

    oa = work.get("open_access") or {}
    if isinstance(oa, dict) and oa.get("oa_url"):
        urls.append(str(oa["oa_url"]))

    ids_block = work.get("ids") or {}
    if isinstance(ids_block, dict) and ids_block.get("doi"):
        urls.append(str(ids_block["doi"]))

    for u in urls:
        canon = _url_to_arxiv_abs(u)
        if canon and urlparse(canon).netloc.lower().endswith("arxiv.org"):
            return canon

    if isinstance(ids_block, dict):
        doi_val = ids_block.get("doi")
        if isinstance(doi_val, str):
            m = _ARXIV_DOI_ID_RE.search(doi_val.lower())
            if m:
                return f"https://arxiv.org/abs/{m.group('id')}"

    return None


def _work_to_item(work: dict[str, Any]) -> Optional[dict[str, str]]:
    link = _canonical_arxiv_abs_url(work)
    if not link:
        return None

    title = (work.get("display_name") or work.get("title") or "").strip()
    if not title:
        return None

    snippet = _reconstruct_abstract(work.get("abstract_inverted_index"))
    if not snippet:
        snippet = title[:600]

    return {"title": title, "link": link, "snippet": snippet}


async def _openalex_get_json(
    session: aiohttp.ClientSession,
    params: dict[str, Any],
    api_key: str,
) -> tuple[Optional[dict[str, Any]], int]:
    q = {**params, "api_key": api_key}
    try:
        async with session.get(OPENALEX_WORKS, params=q, timeout=aiohttp.ClientTimeout(total=45)) as resp:
            status = resp.status
            if status != 200:
                return None, status
            return await resp.json(), status
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None, -1


async def _openalex_fetch_work_encoded(
    session: aiohttp.ClientSession,
    lookup: str,
    api_key: str,
) -> tuple[Optional[dict[str, Any]], int]:
    suffix = quote(lookup.strip(), safe="")
    url = f"{OPENALEX_WORKS}/{suffix}"
    try:
        async with session.get(
            url,
            params={"api_key": api_key},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            status = resp.status
            if status != 200:
                return None, status
            return await resp.json(), status
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None, -1


async def _search_openalex_with_rotation(
    *,
    search_params_base: dict[str, Any],
    max_items: int,
    api_keys: list[str],
) -> list[dict[str, str]]:
    api_keys = list(dict.fromkeys(api_keys))
    if not api_keys:
        raise RuntimeError(
            f"No API keys found in {_KEYS_FILE}. Add OpenAlex API keys (one per line)."
        )

    out: list[dict[str, str]] = []
    key_order = api_keys[:]
    random.shuffle(key_order)

    async with aiohttp.ClientSession(headers={"User-Agent": "desearch-miner-arxiv-openalex/1.0"}) as session:
        for pick in key_order:
            cursor: Optional[str] = "*"

            while cursor and len(out) < max_items:
                with _quota_lock:
                    state = _merge_key_state(api_keys)
                    if _remaining_for_key(state, pick) <= 0:
                        break

                params = {
                    **search_params_base,
                    "filter": ARXIV_SOURCE_FILTER,
                    "per_page": str(min(50, max_items - len(out) + 10)),
                    "cursor": cursor,
                }
                data, status = await _openalex_get_json(session, params, pick)

                if data is None or status != 200:
                    break

                _apply_quota_decrement(pick, api_keys)

                for work in data.get("results") or []:
                    if not isinstance(work, dict):
                        continue
                    item = _work_to_item(work)
                    if item:
                        out.append(item)
                    if len(out) >= max_items:
                        return out[:max_items]

                meta = data.get("meta") or {}
                cursor = meta.get("next_cursor")

            if len(out) >= max_items:
                return out[:max_items]

        if not out:
            raise RuntimeError(
                "OpenAlex arXiv search failed for all keys (quota exhausted, HTTP errors, "
                "or no works mapping to arxiv.org/abs URLs)."
            )
        return out[:max_items]


async def _works_by_ids_openalex(ids: list[str], api_keys: list[str]) -> list[dict[str, str]]:
    api_keys = list(dict.fromkeys(api_keys))
    if not api_keys:
        raise RuntimeError(f"No API keys in {_KEYS_FILE}")

    out: list[dict[str, str]] = []

    async with aiohttp.ClientSession(headers={"User-Agent": "desearch-miner-arxiv-openalex/1.0"}) as session:
        for raw_id in ids:
            rid = str(raw_id).strip()
            if not rid:
                continue

            if re.fullmatch(r"\d{4}\.\d{4,5}(?:v\d+)?", rid):
                lookup = f"https://doi.org/10.48550/arXiv.{rid}"
            elif rid.startswith("http"):
                lookup = rid
            elif rid.startswith("10."):
                lookup = f"https://doi.org/{rid}"
            elif re.fullmatch(r"W\d+", rid):
                lookup = f"https://openalex.org/{rid}"
            else:
                lookup = rid

            random.shuffle(api_keys)
            for pick in api_keys:
                with _quota_lock:
                    state = _merge_key_state(api_keys)
                    if _remaining_for_key(state, pick) <= 0:
                        continue

                data, status = await _openalex_fetch_work_encoded(session, lookup, pick)
                if data is None or status != 200:
                    continue

                _apply_quota_decrement(pick, api_keys)

                item = _work_to_item(data)
                if item:
                    out.append(item)
                break

    return out


async def arxiv_search(q: ArxivQuery) -> list[dict[str, str]]:
    """Return ``SearchResultItem`` dicts for arXiv (OpenAlex-backed)."""

    api_keys = _load_api_keys()
    max_items = max(1, min(int(q.max_items or 10), 100))

    ids = [str(i).strip() for i in (q.id_list or []) if str(i).strip()]
    query = (q.query or "").strip()

    if ids and not query:
        rows = await _works_by_ids_openalex(ids, api_keys)
        return rows[:max_items]

    if not query and not ids:
        return []

    base: dict[str, Any] = {"search": query}
    sort_spec = _SORT_OPENALEX.get(q.sort_by)
    if sort_spec:
        if q.sort_order == "ascending":
            field, _, _ = sort_spec.partition(":")
            base["sort"] = f"{field}:asc"
        else:
            base["sort"] = sort_spec

    return await _search_openalex_with_rotation(
        search_params_base=base,
        max_items=max_items,
        api_keys=api_keys,
    )


async def fill_arxiv_results(
    synapse: Any,
    *,
    query: Optional[str] = None,
    max_items: Optional[int] = None,
    sort_by: SortBy = "relevance",
    sort_order: SortOrder = "descending",
    id_list: Optional[list[str]] = None,
) -> Any:
    """
    Populate ``synapse.arxiv_search_results`` in-place and return synapse.

    Defaults:
    - query from ``synapse.prompt``
    - max_items from ``synapse.max_items`` (fallback 10)
    """
    q = query if query is not None else getattr(synapse, "prompt", "") or ""
    n = max_items if max_items is not None else getattr(synapse, "max_items", None)
    n = int(n or 10)
    results = await arxiv_search(
        ArxivQuery(
            query=str(q),
            max_items=n,
            sort_by=sort_by,
            sort_order=sort_order,
            id_list=id_list,
        )
    )
    setattr(synapse, "arxiv_search_results", results)
    return synapse


def run_arxiv_search_sync(
    query: str,
    *,
    max_items: int = 10,
    sort_by: SortBy = "relevance",
    sort_order: SortOrder = "descending",
    id_list: Optional[list[str]] = None,
) -> list[dict[str, str]]:
    """Synchronous wrapper for quick CLI/manual testing."""
    return asyncio.run(
        arxiv_search(
            ArxivQuery(
                query=query,
                max_items=max_items,
                sort_by=sort_by,
                sort_order=sort_order,
                id_list=id_list,
            )
        )
    )
