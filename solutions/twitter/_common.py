"""
Shared TweetAPI client helpers for Twitter miner solutions.

Backend:
- GET https://api.tweetapi.com/tw-v2/search
- Header: X-API-Key: <TWEET_API_KEY>
- Params: query, type, cursor
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
from openai import AsyncOpenAI

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_ROOT = _REPO_ROOT / "source"

TWEETAPI_URL = "https://api.tweetapi.com/tw-v2/search"
_REQUEST_TIMEOUT_SECONDS = 45
_ALLOWED_TYPES = {"Latest", "Top"}
_MAX_LOOP_PAGES = 10

_ID_RE = re.compile(r"/status/(\d+)")
_ENTRY_TWEET_RE = re.compile(r"^tweet-(\d+)$")
_FROM_USER_RE = re.compile(r"(?:^|\s)from:([A-Za-z0-9_]{1,15})(?:\s|$)", re.IGNORECASE)
_HAS_OPERATOR_RE = re.compile(
    r"(?:^|\s)(from:|to:|since:|until:|lang:|filter:|min_faves:|min_retweets:|min_replies:|url:)",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[A-Za-z0-9_]{3,}")
_STOPWORDS = {
    "will",
    "would",
    "could",
    "should",
    "about",
    "after",
    "before",
    "under",
    "over",
    "into",
    "from",
    "with",
    "that",
    "this",
    "they",
    "them",
    "their",
    "there",
    "where",
    "what",
    "when",
    "which",
    "while",
    "have",
    "has",
    "had",
    "were",
    "been",
    "being",
    "your",
    "you",
    "just",
    "than",
    "then",
    "also",
    "into",
    "onto",
    "upon",
    "will",
    "may",
    "might",
    "can",
    "deliver",
}

_QUERY_GEN_MODEL = "gpt-4.1-nano"
_MAX_GENERATED_QUERIES = 1
_MIN_GENERATED_QUERIES = 1
_QUERY_PLANNER_PROMPT = """You are a Twitter/X search-query generator for TweetAPI.

Your job:
Convert a user sentence, question, or phrase into exactly 1 best Twitter/X search query.

API rules:
- Output ONLY valid JSON.
- Generate only the search query strings for TweetAPI parameter `query`.
- Do NOT generate full URLs.
- Do NOT answer the user’s question.
- Do NOT explain.
- Do NOT include irrelevant keywords.
- Do NOT include too many words.
- Do NOT generate long bag-of-words queries.
- Each query must be optimized for finding relevant tweets/posts.

TweetAPI search endpoint context:
- Endpoint: GET /tw-v2/search
- Params used by caller: query (string), type (Latest|Top), cursor (optional)
- We only need query strings.

Search strategy:
1. Extract only the most important entities:
   - locations
   - people
   - organizations
   - event names
   - dates
   - core action words
2. Remove weak words such as:
   - will, can, should, maybe, after, before, about, information, update, news
3. Use synonyms only when useful.
4. Use OR groups for alternate spellings.
5. Prefer short high-signal queries.
6. Generate 1–2 focused queries instead of many queries.
7. Do NOT use single or double quotes in output queries.
8. Add `lang:en` only if the input is English.
9. Use `-filter:replies` when searching for news/event updates.
10. Do not use `min_faves`, `min_retweets`, or `from:` unless the user asks.
11. Prefer grouping alternatives with OR inside parentheses.
12. Keep each query concise (target <= 12 tokens excluding operators).
13. Do NOT split same-category terms into multiple separate groups.
14. Merge all variants of each category into one OR group per category.
15. Apply category grouping to all categories (locations, organizations, people, events, aid terms, action terms, etc.).
16. Prefer 1-3 strong category groups over many narrow groups.

Relevance rules:
- Every query must directly relate to the original user intent.
- Do not broaden too much.
- Do not add unrelated countries, organizations, or assumptions.
- If location names have alternate spellings, include them with OR.
- If the sentence asks about future or confirmation, search for evidence/events, not the full question.

Output format:
{
  "queries": [
    "..."
  ]
}

Example input:
Will international aid organizations deliver medical supplies to Nabatieh district after Israeli airstrikes on Kafrwa and Al-Namiriya by May 14, 2026?

Example output:
{
  "queries": [
    "(Nabatieh OR Nabatiyeh OR Kafrwa OR Al-Namiriya OR Namiriya) (aid OR humanitarian OR medical supplies) lang:en -filter:replies"
  ]
}
"""


def _debug_enabled() -> bool:
    return (os.environ.get("TWITTER_DEBUG_LOG") or os.environ.get("TWEX_DEBUG_LOG") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _debug_log(message: str, payload: Any | None = None) -> None:
    if not _debug_enabled():
        return
    print(f"[twitter-debug] {message}")
    if payload is not None:
        try:
            print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        except Exception:
            print(str(payload))


def ensure_source_path() -> None:
    if _SOURCE_ROOT.is_dir() and str(_SOURCE_ROOT) not in sys.path:
        sys.path.insert(0, str(_SOURCE_ROOT))


def ensure_desearch_importable() -> None:
    ensure_source_path()
    os.environ.setdefault("OPENAI_API_KEY", "unused-placeholder-twitter-solutions")
    os.environ.setdefault("APIFY_API_KEY", "unused-placeholder-twitter-solutions")


def _require_tweet_api_key() -> str:
    from db.pg import load_env

    load_env()
    key = (os.environ.get("TWEET_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("Set TWEET_API_KEY in environment or repo .env.")
    return key


def _normalize_type(value: Any) -> str:
    s = (str(value).strip() if value is not None else "Latest") or "Latest"
    return s if s in _ALLOWED_TYPES else "Latest"


def _extract_tweet_id_from_url(url: str) -> str | None:
    m = _ID_RE.search(url or "")
    return m.group(1) if m else None


def _entry_ids(entries: list[Any]) -> list[str]:
    ids: list[str] = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        eid = str(e.get("entryId") or "")
        m = _ENTRY_TWEET_RE.match(eid)
        if m:
            ids.append(m.group(1))
    seen: set[str] = set()
    out: list[str] = []
    for x in ids:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _iter_tweet_nodes(obj: Any):
    if isinstance(obj, dict):
        if _looks_like_tweet_node(obj):
            yield obj
        for v in obj.values():
            yield from _iter_tweet_nodes(v)
    elif isinstance(obj, list):
        for it in obj:
            yield from _iter_tweet_nodes(it)


def _looks_like_tweet_node(node: dict[str, Any]) -> bool:
    if node.get("tweet_id") or node.get("id") or node.get("rest_id"):
        if node.get("text") or node.get("full_text") or (isinstance(node.get("legacy"), dict) and (node["legacy"].get("full_text") or node["legacy"].get("text"))):
            return True
    if isinstance(node.get("legacy"), dict) and (node.get("rest_id") or node["legacy"].get("id_str")):
        return True
    return False


def _normalize_api_payload_to_items(payload: Any) -> list[dict[str, Any]]:
    """
    Make best effort to normalize TweetAPI search response into tweet dicts.
    Supports both direct tweet arrays and nested GraphQL-like structures.
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []

    # Common API envelope
    data = payload.get("data")
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]

    # GraphQL-style search responses often carry entries and tweet_results.
    nodes = list(_iter_tweet_nodes(payload))
    if nodes:
        return nodes

    # Fallback: if data is dict and contains arrays.
    if isinstance(data, dict):
        for key in ("tweets", "items", "results"):
            v = data.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def _tweet_node_to_normalized_dict(node: dict[str, Any]) -> dict[str, Any]:
    """
    Convert mixed tweet node shapes into a normalized dictionary used by
    _to_twitter_scraper_tweet.
    """
    leg = node.get("legacy") if isinstance(node.get("legacy"), dict) else {}
    core = node.get("core") if isinstance(node.get("core"), dict) else {}
    user_result = (
        core.get("user_results", {}).get("result", {})
        if isinstance(core.get("user_results"), dict)
        else {}
    )
    user_leg = user_result.get("legacy") if isinstance(user_result.get("legacy"), dict) else {}

    tid = node.get("tweet_id") or node.get("id") or node.get("rest_id") or leg.get("id_str")
    text = node.get("text") or node.get("full_text") or leg.get("full_text") or leg.get("text")

    user = node.get("user") if isinstance(node.get("user"), dict) else {}
    if not user and isinstance(node.get("author"), dict):
        author = node.get("author") or {}
        user = {
            "id": author.get("id"),
            "name": author.get("name"),
            "screen_name": author.get("username"),
            "description": author.get("bio"),
            "created_at": author.get("createdAt"),
            "followers_count": author.get("followerCount"),
            "followings_count": author.get("followingCount"),
            "favourites_count": author.get("favoritesCount"),
            "listed_count": author.get("listedCount"),
            "media_count": author.get("mediaCount"),
            "statuses_count": author.get("tweetCount"),
            "verified": author.get("verified"),
            "is_blue_verified": author.get("isBlueVerified"),
            "profile_image_url": author.get("avatar"),
            "profile_banner_url": author.get("banner"),
            "url": author.get("website"),
            "location": author.get("location"),
            "pinned_tweet_ids": author.get("pinnedTweetIds"),
            "is_translator": author.get("isTranslator"),
            "has_custom_timelines": author.get("hasCustomTimelines"),
        }
    if not user:
        author = node.get("author")
        if isinstance(author, dict):
            user = {
                "id": author.get("id"),
                "name": author.get("name"),
                "screen_name": author.get("screen_name") or author.get("userName") or author.get("username"),
                "description": author.get("description"),
                "created_at": author.get("created_at"),
                "followers_count": author.get("followers_count") or author.get("followers"),
                "favourites_count": author.get("favourites_count") or author.get("favouritesCount"),
                "listed_count": author.get("listed_count") or author.get("listedCount"),
                "media_count": author.get("media_count") or author.get("mediaCount"),
                "statuses_count": author.get("statuses_count") or author.get("statusesCount"),
                "verified": author.get("verified") if author.get("verified") is not None else author.get("isVerified"),
                "is_blue_verified": author.get("is_blue_verified") if author.get("is_blue_verified") is not None else author.get("isBlueVerified"),
                "profile_image_url": author.get("profile_image_url") or author.get("profilePicture"),
                "profile_banner_url": author.get("profile_banner_url") or author.get("coverPicture"),
                "location": author.get("location"),
            }
    if not user and user_leg:
        user = {
            "id": user_result.get("rest_id") or user_leg.get("id_str"),
            "name": user_leg.get("name"),
            "screen_name": user_leg.get("screen_name"),
            "description": user_leg.get("description"),
            "created_at": user_leg.get("created_at"),
            "followers_count": user_leg.get("followers_count"),
            "favourites_count": user_leg.get("favourites_count"),
            "listed_count": user_leg.get("listed_count"),
            "media_count": user_leg.get("media_count"),
            "statuses_count": user_leg.get("statuses_count"),
            "verified": user_leg.get("verified"),
            "is_blue_verified": user_result.get("is_blue_verified"),
            "profile_image_url": user_leg.get("profile_image_url_https"),
            "profile_banner_url": user_leg.get("profile_banner_url"),
            "location": user_leg.get("location"),
        }

    media = node.get("media") if isinstance(node.get("media"), list) else []
    if not media and isinstance(leg.get("extended_entities"), dict):
        media = leg["extended_entities"].get("media") or []

    reply_to = node.get("replyTo") if isinstance(node.get("replyTo"), dict) else {}

    return {
        "tweet_id": str(tid or "").strip(),
        "id": str(tid or "").strip(),
        "text": text,
        "full_text": node.get("full_text") or leg.get("full_text"),
        "reply_count": node.get("replyCount") if node.get("replyCount") is not None else (node.get("reply_count") if node.get("reply_count") is not None else leg.get("reply_count")),
        "retweet_count": node.get("retweetCount") if node.get("retweetCount") is not None else (node.get("retweet_count") if node.get("retweet_count") is not None else leg.get("retweet_count")),
        "favorite_count": node.get("likeCount") if node.get("likeCount") is not None else (node.get("favorite_count") if node.get("favorite_count") is not None else leg.get("favorite_count")),
        "quote_count": node.get("quoteCount") if node.get("quoteCount") is not None else (node.get("quote_count") if node.get("quote_count") is not None else leg.get("quote_count")),
        "bookmark_count": node.get("bookmarkCount") if node.get("bookmarkCount") is not None else node.get("bookmark_count"),
        "view_count": node.get("viewCount") if node.get("viewCount") is not None else node.get("view_count"),
        "created_at": node.get("createdAt") or node.get("created_at") or leg.get("created_at"),
        "is_quote_status": node.get("is_quote_status") if node.get("is_quote_status") is not None else leg.get("is_quote_status"),
        "retweeted_tweet": node.get("retweetedTweet") or node.get("retweeted_tweet") or leg.get("retweeted_status_result") or leg.get("retweeted_status"),
        "lang": node.get("lang") or leg.get("lang"),
        "in_reply_to": (reply_to.get("tweetId") if reply_to else None) or node.get("in_reply_to") or leg.get("in_reply_to_status_id_str"),
        "in_reply_to_user_id": (reply_to.get("userId") if reply_to else None),
        "in_reply_to_screen_name": (reply_to.get("username") if reply_to else None),
        "quoted_status_id_str": (
            (node.get("quotedTweet") or {}).get("id")
            if isinstance(node.get("quotedTweet"), dict)
            else node.get("quoted_status_id_str")
        ) or leg.get("quoted_status_id_str"),
        "thread": node.get("conversationId") or node.get("thread") or node.get("conversation_id") or leg.get("conversation_id_str"),
        "media": media if isinstance(media, list) else [],
        "user": user if isinstance(user, dict) else {},
        "quote": node.get("quotedTweet") if isinstance(node.get("quotedTweet"), dict) else node.get("quote"),
    }


def _extract_from_user_hint(query: str) -> str | None:
    m = _FROM_USER_RE.search(query or "")
    if not m:
        return None
    return m.group(1).strip() or None


def _apply_user_hint(item: dict[str, Any], user_hint: str | None) -> dict[str, Any]:
    if not user_hint:
        return item
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    username = str(user.get("screen_name") or user.get("username") or "").strip()
    name = str(user.get("name") or "").strip()
    uid = str(user.get("id") or "").strip()

    if not username:
        username = user_hint
    if not name:
        name = username
    if not uid:
        uid = f"hint:{username}"

    if not user or not user.get("screen_name") or not user.get("name") or not user.get("id"):
        patched = dict(item)
        patched["user"] = {
            **user,
            "id": uid,
            "screen_name": username,
            "username": username,
            "name": name,
            "verified": user.get("verified"),
            "is_blue_verified": user.get("is_blue_verified"),
            "followers_count": user.get("followers_count"),
            "favourites_count": user.get("favourites_count"),
            "listed_count": user.get("listed_count"),
            "media_count": user.get("media_count"),
            "statuses_count": user.get("statuses_count"),
            "description": user.get("description"),
            "created_at": user.get("created_at"),
            "profile_image_url": user.get("profile_image_url"),
            "profile_banner_url": user.get("profile_banner_url"),
            "location": user.get("location"),
        }
        return patched
    return item


def _to_twitter_scraper_tweet(item: dict, *, is_quote: bool = False):
    if item is None:
        return None

    from desearch.protocol import (
        TwitterScraperMedia,
        TwitterScraperTweet,
        TwitterScraperUser,
    )

    media_list = item.get("media") if isinstance(item.get("media"), list) else []
    media_list = [
        TwitterScraperMedia(
            media_url=media.get("media_url") or media.get("media_url_https") or media.get("url"),
            type=media.get("type"),
        )
        for media in media_list
        if isinstance(media, dict)
    ]

    user_payload = item.get("user") if isinstance(item.get("user"), dict) else {}
    quote = item.get("quote")

    user = None
    if not is_quote and user_payload:
        username = str(user_payload.get("screen_name") or user_payload.get("username") or "").strip()
        uid = str(user_payload.get("id") or "").strip()
        if username and uid:
            user = TwitterScraperUser(
                id=uid,
                created_at=user_payload.get("created_at"),
                description=user_payload.get("description"),
                followers_count=_to_int_or_none(user_payload.get("followers_count")),
                favourites_count=_to_int_or_none(user_payload.get("favourites_count")),
                followings_count=_to_int_or_none(user_payload.get("followings_count")),
                listed_count=_to_int_or_none(user_payload.get("listed_count")),
                media_count=_to_int_or_none(user_payload.get("media_count")),
                statuses_count=_to_int_or_none(user_payload.get("statuses_count")),
                verified=user_payload.get("verified"),
                is_blue_verified=user_payload.get("is_blue_verified"),
                profile_image_url=user_payload.get("profile_image_url"),
                profile_banner_url=user_payload.get("profile_banner_url"),
                url=user_payload.get("url"),
                name=user_payload.get("name") or username,
                username=username,
                entities=None,
                can_dm=user_payload.get("can_dm"),
                can_media_tag=user_payload.get("can_media_tag"),
                location=user_payload.get("location"),
                pinned_tweet_ids=user_payload.get("pinned_tweet_ids"),
            )

    text = str(item.get("text") or item.get("full_text") or "").strip() or "[no text]"
    tid = str(item.get("tweet_id") or item.get("id") or "").strip()
    if not tid:
        return None

    quote_id = item.get("quoted_status_id_str")
    if quote_id is None and isinstance(quote, dict):
        quote_id = quote.get("tweet_id") or quote.get("id")

    return TwitterScraperTweet(
        id=tid,
        text=text,
        reply_count=_to_int(item.get("reply_count")),
        view_count=_to_int_or_none(item.get("view_count")),
        retweet_count=_to_int(item.get("retweet_count")),
        like_count=_to_int(item.get("favorite_count")),
        quote_count=_to_int(item.get("quote_count")),
        bookmark_count=_to_int(item.get("bookmark_count")),
        url=_infer_tweet_url(item),
        created_at=_normalize_created_at(item.get("created_at")),
        is_quote_tweet=item.get("is_quote_status"),
        is_retweet=bool(item.get("retweeted_tweet")),
        media=media_list,
        lang=item.get("lang"),
        conversation_id=_conversation_id(item),
        quote=_to_twitter_scraper_tweet(quote, is_quote=True) if isinstance(quote, dict) else None,
        entities=None,
        extended_entities=None,
        in_reply_to_status_id=item.get("in_reply_to"),
        in_reply_to_screen_name=item.get("in_reply_to_screen_name"),
        in_reply_to_user_id=item.get("in_reply_to_user_id"),
        quoted_status_id=str(quote_id) if quote_id is not None else None,
        user=user,
    )


def _normalize_created_at(value: Any) -> str:
    """
    Validator requires Twitter legacy datetime format:
    '%a %b %d %H:%M:%S %z %Y' (e.g. 'Mon Jun 17 03:51:48 +0000 2024')
    """
    raw = str(value or "").strip()
    if not raw:
        return "Thu Jan 01 00:00:00 +0000 1970"

    # Already in validator format.
    try:
        datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y")
        return raw
    except ValueError:
        pass

    # Common TweetAPI ISO format: 2026-05-02T11:17:01.000Z
    iso_candidate = raw
    if iso_candidate.endswith("Z"):
        iso_candidate = iso_candidate[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(iso_candidate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
        return dt.strftime("%a %b %d %H:%M:%S +0000 %Y")
    except ValueError:
        pass

    # Last-resort fallback to avoid validator crash.
    return "Thu Jan 01 00:00:00 +0000 1970"


def _infer_tweet_url(item: dict) -> str | None:
    tid = item.get("tweet_id") or item.get("id")
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    screen_name = user.get("screen_name") or user.get("username")
    if tid and screen_name:
        return f"https://x.com/{screen_name}/status/{tid}"
    if tid:
        return f"https://x.com/i/status/{tid}"
    return None


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_int_or_none(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _conversation_id(item: dict) -> str | None:
    thread = item.get("thread")
    if isinstance(thread, str):
        return thread.strip() or None
    if isinstance(thread, list) and thread:
        first = thread[0]
        if isinstance(first, dict):
            tid = first.get("tweet_id") or first.get("id")
            return str(tid).strip() if tid else None
        if first is not None and str(first).strip():
            return str(first).strip()
    return None


def _build_search_query_from_run_input(run_input: dict[str, Any]) -> str:
    tweet_ids = run_input.get("tweetIDs")
    if isinstance(tweet_ids, list) and tweet_ids:
        first = str(tweet_ids[0]).strip()
        if first:
            return f"url:https://x.com/i/status/{first}"

    terms = run_input.get("searchTerms")
    if isinstance(terms, list):
        cleaned = [str(t).strip() for t in terms if str(t).strip()]
        if cleaned:
            return _normalize_search_query(" ".join(cleaned))

    content = str(run_input.get("twitterContent") or "").strip()
    if content:
        return _normalize_search_query(content)

    raise RuntimeError("Search query is empty.")


def _normalize_search_query(raw_query: str) -> str:
    """
    Build a TweetAPI-friendly query from arbitrary text.
    - Keep direct Twitter operator queries unchanged.
    - Keep hashtag/cashtag-heavy queries unchanged.
    - For long natural language prompts, convert to compact keywords.
    """
    q = " ".join(str(raw_query or "").strip().split())
    if not q:
        return q

    q_lower = q.lower()
    if _HAS_OPERATOR_RE.search(q):
        return q
    if "#" in q or "$" in q:
        return q

    # Short phrase searches can stay as-is.
    if len(q.split()) <= 8 and "?" not in q:
        return q

    # Preserve quoted segments as high-signal phrases.
    quoted_phrases = re.findall(r'"([^"]{3,80})"', q)

    words = [w.lower() for w in _WORD_RE.findall(q)]
    keywords: list[str] = []
    seen: set[str] = set()
    for w in words:
        if w in _STOPWORDS:
            continue
        if w.isdigit():
            continue
        if w in seen:
            continue
        seen.add(w)
        keywords.append(w)

    # Keep query compact; TweetAPI / X search works better with focused terms.
    keywords = keywords[:8]

    parts: list[str] = []
    for phrase in quoted_phrases[:2]:
        phrase = " ".join(phrase.split()).strip()
        if phrase:
            parts.append(f'"{phrase}"')
    parts.extend(keywords)

    normalized = " ".join(parts).strip()
    return normalized or q


def _is_complex_query(raw_query: str) -> bool:
    q = " ".join(str(raw_query or "").strip().split())
    if not q:
        return False
    if _HAS_OPERATOR_RE.search(q):
        return False
    if "#" in q or "$" in q:
        return False
    # Sentence-like or long comprehensive prompt.
    return ("?" in q) or (len(q.split()) > 8)


def _is_simple_direct_query(raw_query: str) -> bool:
    q = " ".join(str(raw_query or "").strip().split())
    if not q:
        return False
    if _HAS_OPERATOR_RE.search(q):
        return True
    if q.startswith("#") or q.startswith("$"):
        return True
    if q.startswith("@"):
        return True
    if len(q.split()) <= 3 and "?" not in q:
        return True
    return False


def _looks_like_long_word_bag(query: str) -> bool:
    q = " ".join(str(query or "").strip().split())
    if not q:
        return False
    if " OR " in q or "(" in q or ")" in q:
        return False
    tokens = q.split()
    return len(tokens) > 10


def _sanitize_api_query(query: str) -> str:
    s = " ".join(str(query or "").strip().split())
    # TweetAPI query parsing can be brittle with quotes; strip both kinds.
    s = s.replace('"', "").replace("'", "")
    # Clean accidental empty parenthesis groups that may remain.
    s = re.sub(r"\(\s*\)", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _build_openai_client() -> AsyncOpenAI | None:
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        return None
    return AsyncOpenAI(api_key=key, timeout=45.0)


async def _llm_generate_queries(user_input: str) -> list[str]:
    client = _build_openai_client()
    if client is None:
        return []
    try:
        response = await client.chat.completions.create(
            model=_QUERY_GEN_MODEL,
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _QUERY_PLANNER_PROMPT},
                {"role": "user", "content": f"Now convert this input:\n{user_input}"},
            ],
        )
        content = response.choices[0].message.content or ""
        parsed = json.loads(content)
        queries = parsed.get("queries")
        if not isinstance(queries, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for q in queries:
            s = _sanitize_api_query(str(q or ""))
            if not s:
                continue
            # Enforce prompt contract: keep concise and OR-structured for complex intent.
            if _looks_like_long_word_bag(s):
                continue
            if s in seen:
                continue
            seen.add(s)
            out.append(s)
        if len(out) < _MIN_GENERATED_QUERIES:
            return []
        return out[:_MAX_GENERATED_QUERIES]
    except Exception as e:
        _debug_log("llm_query_generation_error", {"error": str(e)})
        return []


def _build_search_type_from_run_input(run_input: dict[str, Any]) -> str:
    return _normalize_type(run_input.get("queryType") or run_input.get("sortBy"))


def _extract_cursor(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("next_cursor", "cursor", "nextCursor"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("next_cursor", "cursor", "nextCursor"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    pagination = payload.get("pagination")
    if isinstance(pagination, dict):
        for key in ("nextCursor", "next_cursor", "cursor"):
            value = pagination.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


async def _call_search_once(api_key: str, query: str, search_type: str, cursor: str | None) -> Any:
    timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS)
    headers = {"X-API-Key": api_key, "Accept": "application/json"}
    params: dict[str, str] = {
        "query": query,
        "type": _normalize_type(search_type),
    }
    if cursor:
        params["cursor"] = cursor

    _debug_log("tweetapi_request", {"url": TWEETAPI_URL, "params": params, "headers": {"X-API-Key": f"{api_key[:6]}...{api_key[-4:] if len(api_key) >= 4 else ''}"}})
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(TWEETAPI_URL, params=params, headers=headers) as resp:
            text = await resp.text()
            _debug_log("tweetapi_response_meta", {"status": resp.status, "text_preview": text[:1000]})
            if resp.status >= 400:
                raise RuntimeError(f"TweetAPI HTTP {resp.status}: {text[:500]}")
            try:
                payload = await resp.json(content_type=None)
            except Exception:
                payload = {"raw_text": text}
            _debug_log("tweetapi_response_json", payload)
            return payload


async def run_actor_to_result_dicts(actor_id: str, run_input: dict) -> list[dict]:
    """
    Backward-compatible entrypoint used by ``solutions.twitter.query/id/url``.
    ``actor_id`` is ignored.
    """
    del actor_id
    ensure_desearch_importable()

    api_key = _require_tweet_api_key()
    raw_query = (
        " ".join([str(t).strip() for t in run_input.get("searchTerms", []) if str(t).strip()])
        if isinstance(run_input.get("searchTerms"), list) and run_input.get("searchTerms")
        else str(run_input.get("twitterContent") or "").strip()
    )
    query = _build_search_query_from_run_input(run_input)
    user_hint = _extract_from_user_hint(query)
    search_type = _build_search_type_from_run_input(run_input)
    target_count = max(1, _to_int(run_input.get("maxItems"), default=20))

    query_candidates: list[str] = [query]
    if not _is_simple_direct_query(raw_query):
        llm_queries = await _llm_generate_queries(raw_query)
        if llm_queries:
            # Use only one best LLM-generated query for complex prompts.
            query_candidates = [llm_queries[0]]
    query_candidates = [_sanitize_api_query(query_candidates[0])]

    _debug_log(
        "prepared_run",
        {
            "run_input": run_input,
            "raw_query": raw_query,
            "query": query,
            "query_candidates": query_candidates,
            "user_hint": user_hint,
            "type": search_type,
            "target_count": target_count,
        },
    )

    # Always show effective API query for complex sentence-like tasks.
    if _is_complex_query(raw_query):
        print(
            f"[twitter-query] original={raw_query!r} | api_query={query_candidates[0]!r}"
        )

    collected_raw: list[dict[str, Any]] = []
    seen_raw_ids: set[str] = set()
    planned_query = query_candidates[0]
    payload = await _call_search_once(api_key, planned_query, search_type, None)
    items = _normalize_api_payload_to_items(payload)
    _debug_log(
        "normalized_page_items",
        {
            "query": planned_query,
            "page": 1,
            "count": len(items),
            "cursor_in": None,
        },
    )

    for node in items:
        n = _tweet_node_to_normalized_dict(node)
        rid = str(n.get("tweet_id") or n.get("id") or "").strip()
        if not rid:
            continue
        if rid in seen_raw_ids:
            continue
        seen_raw_ids.add(rid)
        collected_raw.append(n)
        if len(collected_raw) >= target_count:
            break

    # If search came from tweetIDs, enforce exact id filtering.
    tweet_ids = run_input.get("tweetIDs")
    if isinstance(tweet_ids, list) and tweet_ids:
        wanted = {str(t).strip() for t in tweet_ids if str(t).strip()}
        collected_raw = [x for x in collected_raw if str(x.get("tweet_id") or x.get("id") or "").strip() in wanted]

    out: list[dict] = []
    convert_errors = 0
    for item in collected_raw:
        try:
            tweet = _to_twitter_scraper_tweet(_apply_user_hint(item, user_hint))
            if tweet is not None:
                out.append(tweet.model_dump(mode="json"))
        except Exception:
            convert_errors += 1
            continue
    _debug_log("conversion_result", {"raw_count": len(collected_raw), "out_count": len(out), "convert_errors": convert_errors})
    return out[:target_count]


def new_actor_search_input_from_synapse(synapse: Any) -> dict:
    """
    Map ``TwitterSearchSynapse`` fields to internal run_input.
    """
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
    if start and "since:" not in q_lower:
        parts.append(f"since:{start}")
    if end and "until:" not in q_lower:
        parts.append(f"until:{end}")

    lang = (getattr(synapse, "lang", None) or "").strip()
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

    final_query = " ".join(parts).strip() or query
    count = max(1, int(getattr(synapse, "count", 20)))
    sort = _normalize_type(getattr(synapse, "sort", None))

    return {
        "tweetIDs": [],
        "twitterContent": final_query,
        "searchTerms": [final_query] if final_query else [],
        "maxItems": count,
        "queryType": sort,
    }


# Compatibility exports expected by query/id/url modules.
NEW_ACTOR_ID = "TWEETAPI"
LEGACY_ACTOR_ID = "TWEETAPI"


def legacy_actor_search_input_from_synapse(synapse: Any) -> dict:
    return new_actor_search_input_from_synapse(synapse)


def use_legacy_actor_from_env() -> bool:
    return False
