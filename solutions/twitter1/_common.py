"""
Twikit-backed Twitter helpers (no Apify).

Credentials are read from environment:
- ``TWITTER_USERNAME``
- ``TWITTER_PASSWORD``
- ``TWITTER_EMAIL``

Session cookies are cached in ``solutions/twitter1/.twikit-cookies.json``.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

_COOKIES_FILE = Path(__file__).resolve().parent / ".twikit-cookies.json"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE = _REPO_ROOT / ".env"
_ID_RE = re.compile(r"/status/(\d+)")

_CLIENTS: dict[str, Any] = {}
_CLIENT_LOCK = asyncio.Lock()


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
        raise RuntimeError(f"Set {name} in environment for Twikit login.")
    return value


def _extract_tweet_id(url: str) -> str | None:
    m = _ID_RE.search(url or "")
    return m.group(1) if m else None


def _get_attr(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return default


def _tweet_to_dict(tweet: Any) -> dict[str, Any]:
    user = _get_attr(tweet, "user", default=None)
    user_name = _get_attr(user, "screen_name", "username", "name", default="")
    tweet_id = str(_get_attr(tweet, "id", "id_str", default="") or "")
    url = _get_attr(tweet, "url", default=None)
    if not url and tweet_id:
        if user_name:
            url = f"https://x.com/{user_name}/status/{tweet_id}"
        else:
            url = f"https://x.com/i/status/{tweet_id}"

    created_at = _get_attr(tweet, "created_at", default="")
    return {
        "id": tweet_id,
        "text": _get_attr(tweet, "text", "full_text", default=""),
        "reply_count": int(_get_attr(tweet, "reply_count", default=0) or 0),
        "view_count": _get_attr(tweet, "view_count", default=None),
        "retweet_count": int(_get_attr(tweet, "retweet_count", default=0) or 0),
        "like_count": int(
            _get_attr(tweet, "favorite_count", "like_count", default=0) or 0
        ),
        "quote_count": int(_get_attr(tweet, "quote_count", default=0) or 0),
        "bookmark_count": int(_get_attr(tweet, "bookmark_count", default=0) or 0),
        "url": url,
        "created_at": str(created_at) if created_at is not None else "",
        "is_quote_tweet": bool(_get_attr(tweet, "is_quote_status", default=False)),
        "is_retweet": bool(_get_attr(tweet, "is_retweet", default=False)),
        "media": [],
        "lang": _get_attr(tweet, "lang", default=None),
        "conversation_id": _get_attr(tweet, "conversation_id", default=None),
        "in_reply_to_status_id": _get_attr(tweet, "in_reply_to_status_id", default=None),
        "quoted_status_id": _get_attr(tweet, "quoted_status_id", default=None),
        "user": None,
    }


async def _get_client(proxy: str | None = None):
    key = (proxy or "").strip()
    if key in _CLIENTS:
        return _CLIENTS[key]
    async with _CLIENT_LOCK:
        if key in _CLIENTS:
            return _CLIENTS[key]
        try:
            from twikit import Client
        except Exception as e:
            raise RuntimeError(
                "Twikit is required. Install with: poetry add twikit"
            ) from e

        client = Client("en-US", proxy=proxy or None)
        if _COOKIES_FILE.exists():
            try:
                client.load_cookies(str(_COOKIES_FILE))
                _CLIENTS[key] = client
                return _CLIENTS[key]
            except Exception:
                pass

        username = _require_env("TWITTER_USERNAME")
        password = _require_env("TWITTER_PASSWORD")
        email = _require_env("TWITTER_EMAIL")

        await client.login(
            auth_info_1=username,
            auth_info_2=email,
            password=password,
        )
        try:
            client.save_cookies(str(_COOKIES_FILE))
        except Exception:
            pass
        _CLIENTS[key] = client
        return _CLIENTS[key]


def build_twikit_query_from_synapse(synapse: Any) -> str:
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


async def search_tweets(
    query: str,
    product: str,
    count: int,
    proxy: str | None = None,
) -> list[dict[str, Any]]:
    client = await _get_client(proxy=proxy)
    twikit_product = product if product in ("Top", "Latest", "Media") else "Latest"
    tweets = await client.search_tweet(query, twikit_product, count=max(1, int(count)))
    return [_tweet_to_dict(t) for t in tweets][: max(1, int(count))]


async def tweets_by_ids(tweet_ids: list[str]) -> list[dict[str, Any]]:
    ids = [str(i).strip() for i in tweet_ids if str(i).strip()]
    if not ids:
        return []
    client = await _get_client()
    tweets = await client.get_tweets_by_ids(ids)
    return [_tweet_to_dict(t) for t in tweets]


async def tweets_by_urls(urls: list[str]) -> list[dict[str, Any]]:
    ids = [_extract_tweet_id(u) for u in urls]
    return await tweets_by_ids([i for i in ids if i])
