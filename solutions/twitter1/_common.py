"""
Twitter/X helpers using ``twitter-api-client`` (PyPI).

Credentials from environment (loaded from repo ``.env`` if unset):
- ``TWITTER_EMAIL``
- ``TWITTER_USERNAME``
- ``TWITTER_PASSWORD``

Optional session cache: ``solutions/twitter1/.twitter-api-client.cookies``
(JSON cookies file as supported by the library).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE = _REPO_ROOT / ".env"
_COOKIES_FILE = Path(__file__).resolve().parent / ".twitter-api-client.cookies"
_SEARCH_TMP = Path(__file__).resolve().parent / "_search_tmp"

_ID_RE = re.compile(r"/status/(\d+)")
_ENTRY_TWEET = re.compile(r"^tweet-(\d+)$")

_SCRAPER: Any = None


def _load_repo_env() -> None:
    if not _ENV_FILE.is_file():
        return
    for raw in _ENV_FILE.read_text(encoding="utf-8").splitlines():
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


def _require_env(name: str) -> str:
    _load_repo_env()
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(
            f"Set {name} in environment (or repo .env) for twitter-api-client login."
        )
    return value


def _extract_tweet_id(url: str) -> str | None:
    m = _ID_RE.search(url or "")
    return m.group(1) if m else None


def _ensure_scraper() -> Any:
    global _SCRAPER
    if _SCRAPER is not None:
        return _SCRAPER
    try:
        from twitter.scraper import Scraper
    except ImportError as e:
        raise RuntimeError(
            "twitter-api-client is required. Install with: poetry add twitter-api-client"
        ) from e

    try:
        if _COOKIES_FILE.is_file():
            _SCRAPER = Scraper(cookies=str(_COOKIES_FILE))
        else:
            _SCRAPER = Scraper(
                _require_env("TWITTER_EMAIL"),
                _require_env("TWITTER_USERNAME"),
                _require_env("TWITTER_PASSWORD"),
            )
    except json.JSONDecodeError as e:
        raise RuntimeError(
            "Twitter/X login returned invalid JSON (empty or blocked response). "
            "Password login is unreliable; export `ct0` and `auth_token` from your browser "
            f"into JSON and save as {_COOKIES_FILE} "
            "(see twitter-api-client docs: cookies=… / cookies file)."
        ) from e
    try:
        _SCRAPER.save_cookies(str(_COOKIES_FILE))
    except Exception:
        pass
    return _SCRAPER


def _iter_tweet_nodes(obj: Any) -> Any:
    if isinstance(obj, dict):
        leg = obj.get("legacy")
        if (
            isinstance(leg, dict)
            and "full_text" in leg
            and (obj.get("rest_id") or leg.get("id_str"))
        ):
            yield obj
        for v in obj.values():
            yield from _iter_tweet_nodes(v)
    elif isinstance(obj, list):
        for x in obj:
            yield from _iter_tweet_nodes(x)


def _screen_name_from_node(node: dict) -> str:
    try:
        u = node.get("core", {}).get("user_results", {}).get("result", {})
        if isinstance(u, dict):
            leg = u.get("legacy") or {}
            if isinstance(leg, dict) and leg.get("screen_name"):
                return str(leg["screen_name"])
        u2 = node.get("author", {})
        if isinstance(u2, dict) and u2.get("username"):
            return str(u2["username"])
    except Exception:
        pass
    return ""


def _tweet_node_to_dict(node: dict) -> dict[str, Any]:
    leg = node.get("legacy") or {}
    tid = str(node.get("rest_id") or leg.get("id_str") or "").strip()
    if not tid:
        return {}
    text = str(leg.get("full_text") or leg.get("text") or "")
    user_sn = _screen_name_from_node(node)
    url = (
        f"https://x.com/{user_sn}/status/{tid}"
        if user_sn
        else f"https://x.com/i/status/{tid}"
    )
    return {
        "id": tid,
        "text": text,
        "reply_count": int(leg.get("reply_count") or 0),
        "view_count": leg.get("ext_views", {}).get("count")
        if isinstance(leg.get("ext_views"), dict)
        else None,
        "retweet_count": int(leg.get("retweet_count") or 0),
        "like_count": int(leg.get("favorite_count") or leg.get("favourite_count") or 0),
        "quote_count": int(leg.get("quote_count") or 0),
        "bookmark_count": int(leg.get("bookmark_count") or 0),
        "url": url,
        "created_at": str(leg.get("created_at") or ""),
        "is_quote_tweet": bool(leg.get("is_quote_status")),
        "is_retweet": bool(leg.get("retweeted_status_result") or leg.get("retweeted_status")),
        "media": [],
        "lang": leg.get("lang"),
        "conversation_id": str(leg.get("conversation_id_str") or "")
        or None,
        "in_reply_to_status_id": leg.get("in_reply_to_status_id_str"),
        "quoted_status_id": None,
        "user": None,
    }


def _normalize_scraper_payload(raw: Any) -> list[dict[str, Any]]:
    chunks: list[dict] = []
    if isinstance(raw, dict):
        chunks = [raw]
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                chunks.append(item)
            elif isinstance(item, list):
                for sub in item:
                    if isinstance(sub, dict):
                        chunks.append(sub)

    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for chunk in chunks:
        for node in _iter_tweet_nodes(chunk):
            row = _tweet_node_to_dict(node)
            tid = row.get("id")
            if tid and tid not in seen:
                seen.add(str(tid))
                out.append(row)
    return out


def _entry_ids(entries: list[Any]) -> list[str]:
    ids: list[str] = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        eid = str(e.get("entryId") or "")
        m = _ENTRY_TWEET.match(eid)
        if m:
            ids.append(m.group(1))
    seen: set[str] = set()
    ordered: list[str] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            ordered.append(i)
    return ordered


def _sort_to_category(sort: str | None) -> str:
    s = (sort or "Latest").strip()
    if s == "Top":
        return "Top"
    if s in ("Photos", "Media"):
        return "Photos"
    if s == "Videos":
        return "Videos"
    if s == "People":
        return "People"
    return "Latest"


def _search_sync(query: str, category: str, limit: int) -> list[dict[str, Any]]:
    from twitter.search import Search

    scraper = _ensure_scraper()
    _SEARCH_TMP.mkdir(parents=True, exist_ok=True)
    search = Search(session=scraper.session, save=False, debug=0)
    lim = max(1, min(int(limit), 500))
    res = search.run(
        queries=[{"category": category, "query": query}],
        limit=lim,
        retries=3,
        out=str(_SEARCH_TMP),
    )
    if not res:
        return []
    entries = res[0] if isinstance(res[0], list) else []
    ids = _entry_ids(entries)[:lim]
    if not ids:
        return []
    raw = scraper.tweets_by_ids(ids)
    return _normalize_scraper_payload(raw)


async def search_tweets(
    query: str,
    product: str,
    count: int,
) -> list[dict[str, Any]]:
    category = _sort_to_category(product)

    def _run() -> list[dict[str, Any]]:
        return _search_sync(query, category, max(1, int(count)))

    return await asyncio.to_thread(_run)


async def tweets_by_ids(tweet_ids: list[str]) -> list[dict[str, Any]]:
    ids = [str(i).strip() for i in tweet_ids if str(i).strip()]
    if not ids:
        return []

    def _run() -> list[dict[str, Any]]:
        scraper = _ensure_scraper()
        raw = scraper.tweets_by_ids(ids)
        normalized = _normalize_scraper_payload(raw)
        id_order = {tid: idx for idx, tid in enumerate(ids)}
        normalized.sort(key=lambda r: id_order.get(str(r.get("id")), 9999))
        return normalized

    return await asyncio.to_thread(_run)


async def tweets_by_urls(urls: list[str]) -> list[dict[str, Any]]:
    ids = [_extract_tweet_id(u) for u in urls]
    return await tweets_by_ids([i for i in ids if i])


def build_query_from_synapse(synapse: Any) -> str:
    query = (getattr(synapse, "query", None) or "").strip()
    user = (getattr(synapse, "user", None) or "").strip()
    q_lower = query.lower()
    parts: list[str] = []
    if query:
        parts.append(query)
    if user and "from:" not in q_lower:
        parts.append(f"from:{user}")
    if getattr(synapse, "verified", None) and "filter:verified" not in q_lower:
        parts.append("filter:verified")
    if getattr(synapse, "blue_verified", None):
        parts.append("filter:blue_verified")
    if getattr(synapse, "is_quote", None):
        parts.append("filter:quote")
    if getattr(synapse, "is_image", None):
        parts.append("filter:images")
    if getattr(synapse, "is_video", None):
        parts.append("filter:videos")
    start = getattr(synapse, "start_date", None)
    end = getattr(synapse, "end_date", None)
    lang = (getattr(synapse, "lang", None) or "").strip()
    if start and "since:" not in q_lower:
        parts.append(f"since:{start}")
    if end and "until:" not in q_lower:
        parts.append(f"until:{end}")
    if lang and "lang:" not in q_lower:
        parts.append(f"lang:{lang}")
    mr = getattr(synapse, "min_retweets", None)
    if mr is not None and "min_retweets:" not in q_lower:
        parts.append(f"min_retweets:{int(mr)}")
    ml = getattr(synapse, "min_likes", None)
    if ml is not None and "min_faves:" not in q_lower:
        parts.append(f"min_faves:{int(ml)}")
    mre = getattr(synapse, "min_replies", None)
    if mre is not None and "min_replies:" not in q_lower:
        parts.append(f"min_replies:{int(mre)}")
    return " ".join(parts).strip()
