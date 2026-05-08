"""
Reddit post search via Arctic Shift (`/api/posts/search`_).

Produces ``SearchResultItem`` shapes: ``{title, link, snippet}``, with ``link``
pointing at the **reddit.com** discussion URL (from ``permalink``) so
``search_content_relevance`` attribution and Apify scraping see ``reddit.com``.

API rules (keyword ``query``, ``title``, ``selftext``): must combine with
``author`` **or** ``subreddit``.
See `<https://github.com/ArthurHeitmann/arctic_shift/blob/master/api/README.md>`_.

Base URL: https://arctic-shift.photon-reddit.com
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Optional
from urllib.parse import urlencode

import aiohttp

ARCTIC_POSTS_SEARCH = "https://arctic-shift.photon-reddit.com/api/posts/search"

_DEFAULT_SUBREDDITS: tuple[str, ...] = (
    "technology",
    "science",
    "explainlikeimfive",
    "AskReddit",
    "todayilearned",
    "news",
)

SortOrder = Literal["asc", "desc"]

_SUBREDDIT_RE = re.compile(
    r"""(?:^|\s|\()/
        r/
        ([A-Za-z0-9_]{2,})
    """,
    re.VERBOSE | re.IGNORECASE,
)


def parse_subreddit_from_prompt(prompt: str) -> Optional[str]:
    """First ``r/sub`` in prompt → subreddit name (no prefix)."""
    m = _SUBREDDIT_RE.search(prompt or "")
    return m.group(1) if m else None


def reddit_discussion_url(post: dict[str, Any]) -> str:
    """
    Prefer ``permalink`` so link posts still score as reddit.com (not CNN etc.).
    """
    permalink = (post.get("permalink") or "").strip()
    if permalink.startswith("/"):
        return f"https://www.reddit.com{permalink.split('?', 1)[0].rstrip('/')}"
    u = (post.get("url") or "").strip()
    if u.startswith("http") and "reddit.com" in u:
        return u.split("?", 1)[0].rstrip("/")
    pid = str(post.get("id") or "").strip().lstrip("t3_")
    sub = (post.get("subreddit") or "").strip()
    if pid and sub:
        return f"https://www.reddit.com/r/{sub}/comments/{pid}/"
    return ""


def _snippet_from_post(post: dict[str, Any]) -> str:
    meta = post.get("_meta")
    title = (post.get("title") or "").strip()
    if isinstance(meta, dict) and meta.get("edited_title"):
        title = (meta.get("edited_title") or title).strip()
    body = (post.get("selftext") or "").strip()
    flair = (post.get("link_flair_text") or "").strip()
    sub = (post.get("subreddit") or "").strip()
    au = (post.get("author") or "").strip()
    ts = post.get("created_utc")

    parts: list[str] = []
    if sub:
        parts.append(f"r/{sub}")
    if au:
        parts.append(f"u/{au}")
    if isinstance(ts, (int, float)):
        parts.append(datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d"))
    head = " | ".join(parts)
    if flair:
        head = f"[{flair}] {head}" if head else f"[{flair}]"

    text = title
    if body:
        text = f"{text}\n\n{body}" if text else body
    if head:
        text = f"{head}\n{text}" if text else head
    text = text.replace("\r\n", "\n").strip()
    if len(text) > 2000:
        text = text[:1997].rstrip() + "..."
    return text


def _skipped_post(post: dict[str, Any]) -> bool:
    meta = post.get("_meta")
    if isinstance(meta, dict):
        if meta.get("was_deleted_later"):
            return True
        if meta.get("removal_type") == "moderator":
            return True
    title = str(post.get("title") or "")
    body = str(post.get("selftext") or "")
    bad_titles = (
        "[deleted]",
        "[removed]",
        "[ Removed by moderator ]",
    )
    if any(b in title for b in bad_titles):
        return True
    low = (title + body).lower()
    if "removed by reddit" in low:
        return True
    return False


def _post_to_item(post: dict[str, Any]) -> Optional[dict[str, str]]:
    if _skipped_post(post):
        return None
    link = reddit_discussion_url(post)
    title = str(post.get("title") or "").strip()
    meta = post.get("_meta") if isinstance(post.get("_meta"), dict) else {}
    if meta.get("edited_title"):
        cand = str(meta["edited_title"]).strip()
        if cand and "[removed" not in cand.lower():
            title = cand or title
    if not link or not title:
        return None
    snippet = _snippet_from_post(post) or title
    return {"title": title, "link": link, "snippet": snippet}


def _text_matches(prompt_lower: str, post: dict[str, Any]) -> bool:
    if not prompt_lower:
        return True
    blob = (
        str(post.get("title") or "")
        + " "
        + str(post.get("selftext") or "")
        + " "
        + str(post.get("link_flair_text") or "")
    ).lower()
    tokens = [t for t in prompt_lower.split() if len(t) > 2]
    if prompt_lower.strip() and prompt_lower.strip() in blob:
        return True
    return bool(tokens) and sum(1 for t in tokens if t in blob) >= min(
        2, len(tokens)
    )


@dataclass(frozen=True)
class RedditQuery:
    """Arctic Shift post search."""

    query: str
    max_items: int = 10
    """Target number of Reddit posts (≤100)."""

    subreddit: Optional[str] = None
    """If set, search only this subreddit (omit fan-out)."""

    subreddits: Optional[tuple[str, ...]] = None
    """When ``subreddit`` is None and this is None, defaults are used."""

    sort: SortOrder = "desc"
    after: Optional[str] = None
    before: Optional[str] = None


async def _fetch_posts(
    session: aiohttp.ClientSession,
    *,
    subreddit: str,
    query: Optional[str],
    limit: int,
    sort: SortOrder,
    after: Optional[str],
    before: Optional[str],
) -> list[dict[str, Any]]:
    sub = subreddit.strip().lstrip("r/")
    if not sub:
        return []

    params: dict[str, Any] = {
        "subreddit": sub,
        "sort": sort if sort in ("asc", "desc") else "desc",
        "limit": str(max(1, min(int(limit), 100))),
    }
    if query:
        params["query"] = query.strip()
    if after:
        params["after"] = after
    if before:
        params["before"] = before

    url = f"{ARCTIC_POSTS_SEARCH}?{urlencode(params)}"

    timeout = aiohttp.ClientTimeout(total=35)
    try:
        async with session.get(url, timeout=timeout) as resp:
            if resp.status != 200:
                return []
            payload = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return []

    if not isinstance(payload, dict) or payload.get("error"):
        return []
    data = payload.get("data") or []
    return [p for p in data if isinstance(p, dict)]


async def arctic_reddit_posts_search(q: RedditQuery) -> list[dict[str, str]]:
    """Return ``SearchResultItem`` dicts for Reddit Search tool."""

    max_items = max(1, min(int(q.max_items or 10), 100))

    subs: tuple[str, ...]
    if q.subreddit and str(q.subreddit).strip():
        subs = (q.subreddit.lstrip("r/"),)
    else:
        subs = q.subreddits if q.subreddits else _DEFAULT_SUBREDDITS

    kw = (q.query or "").strip()
    overlap_prompt = kw.lower()
    per_sub_limit = max(1, min(100, (max_items + len(subs) - 1) // max(1, len(subs)) + 5))

    timeout = aiohttp.ClientTimeout(total=45)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        tasks = [
            _fetch_posts(
                session,
                subreddit=sub,
                query=kw if kw else None,
                limit=per_sub_limit,
                sort=q.sort,
                after=q.after,
                before=q.before,
            )
            for sub in subs
        ]
        batch = await asyncio.gather(*tasks)

    merged: dict[str, dict[str, Any]] = {}
    for rows in batch:
        for post in rows:
            pid = str(post.get("id") or post.get("name") or "").lstrip("t3_")
            if not pid:
                continue
            key = f"{post.get('subreddit', '')}:{pid}"
            merged[key] = post

    scored: list[tuple[float, dict[str, str]]] = []
    for post in merged.values():
        if kw and not _text_matches(overlap_prompt, post):
            continue
        item = _post_to_item(post)
        if not item:
            continue
        ts = post.get("created_utc")
        score_t = float(ts) if isinstance(ts, (int, float)) else 0.0
        scored.append((score_t, item))

    reverse = q.sort == "desc"
    scored.sort(key=lambda x: x[0], reverse=reverse)

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for _, item in scored:
        link = item["link"]
        if link in seen:
            continue
        seen.add(link)
        out.append(item)
        if len(out) >= max_items:
            break

    if len(out) < min(3, max_items) and kw:
        for post in merged.values():
            item = _post_to_item(post)
            if not item:
                continue
            if item["link"] in seen:
                continue
            seen.add(item["link"])
            out.append(item)
            if len(out) >= max_items:
                break

    return out


def _strip_subreddit_mentions(prompt: str, sub: str) -> str:
    p = prompt
    for pat in (f"r/{sub}", f"/r/{sub}", f"r/{sub.lower()}"):
        p = re.sub(re.escape(pat), " ", p, flags=re.IGNORECASE)
    return " ".join(p.split()).strip()


async def fill_reddit_results(
    synapse: Any,
    *,
    query: Optional[str] = None,
    max_items: Optional[int] = None,
    subreddit: Optional[str] = None,
    sort: SortOrder = "desc",
) -> Any:
    """
    Set ``synapse.reddit_search_results`` from Arctic Shift.

    - ``query`` defaults to ``synapse.prompt``.
    - ``max_items`` defaults to ``synapse.max_items`` or 10.
    - If ``subreddit`` is unset, uses first ``r/...`` in the prompt, else
      multi-subreddit fan-out with client-side keyword overlap.
    """

    prompt = str(getattr(synapse, "prompt", "") or "")
    qtext = query if query is not None else prompt

    inferred = parse_subreddit_from_prompt(prompt)
    explicit = (
        subreddit.strip().lstrip("r/")
        if subreddit and str(subreddit).strip()
        else None
    )
    target_sub = explicit or inferred

    kw = qtext.strip()
    if target_sub:
        kw = _strip_subreddit_mentions(kw, target_sub)

    n = max_items if max_items is not None else getattr(synapse, "max_items", None)
    n = int(n or 10)

    rq = RedditQuery(
        query=kw,
        max_items=n,
        subreddit=target_sub,
        subreddits=None if target_sub else _DEFAULT_SUBREDDITS,
        sort=sort,
    )
    results = await arctic_reddit_posts_search(rq)
    setattr(synapse, "reddit_search_results", results)
    return synapse


def run_arctic_reddit_search_sync(
    query: str,
    *,
    max_items: int = 10,
    subreddit: Optional[str] = None,
    sort: SortOrder = "desc",
) -> list[dict[str, str]]:
    """Sync helper for quick CLI tests."""
    return asyncio.run(
        arctic_reddit_posts_search(
            RedditQuery(
                query=query,
                max_items=max_items,
                subreddit=subreddit.lstrip("r/") if subreddit else None,
                subreddits=None if subreddit else _DEFAULT_SUBREDDITS,
                sort=sort,
            )
        )
    )
