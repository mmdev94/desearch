"""
Shared Apify client helpers for Twitter miner solutions.

- **New actor** ``CJdippxWmn9uRfooo`` — search via ``twitterContent`` / ``searchTerms`` and
  fetch by ``tweetIDs`` (same actor validators use in ``get_tweets``).
- **Legacy actor** ``61RPP7dywgiy0JPD0`` — optional search via ``searchTerms`` /
  ``twitterHandles`` / ``startUrls`` (console input style you provided).

Requires ``APIFY_API_KEY``. Imports ``toTwitterScraperTweet`` from the validator module;
``OPENAI_API_KEY`` is set to a placeholder only if unset so ``desearch`` can import.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_ROOT = _REPO_ROOT / "source"

NEW_ACTOR_ID = "CJdippxWmn9uRfooo"
LEGACY_ACTOR_ID = "61RPP7dywgiy0JPD0"
_CREDIT_PER_1000_TWEETS = 0.25
_APIFY_CREDIT_LIMIT = 5.0
_ALLOWED_LANGS = {
    "am",
    "ar",
    "bg",
    "bn",
    "bo",
    "ca",
    "ch`",
    "cs",
    "da",
    "de",
    "dv",
    "el",
    "en",
    "es",
    "et",
    "fa",
    "fi",
    "fr",
    "gu",
    "hi",
    "ht",
    "hu",
    "hy",
    "in",
    "is",
    "it",
    "iu",
    "iw",
    "ja",
    "ka",
    "km",
    "kn",
    "ko",
    "lo",
    "lt",
    "lv",
    "ml",
    "my",
    "ne",
    "nl",
    "no",
    "or",
    "pa",
    "pl",
    "pt",
    "ro",
    "ru",
    "si",
    "sk",
    "sl",
    "sv",
    "ta",
    "te",
    "th",
    "tl",
    "tr",
    "uk",
    "ur",
    "vi",
    "zh",
}


def ensure_source_path() -> None:
    if _SOURCE_ROOT.is_dir() and str(_SOURCE_ROOT) not in sys.path:
        sys.path.insert(0, str(_SOURCE_ROOT))


def ensure_desearch_importable() -> None:
    ensure_source_path()
    # Some desearch imports expect APIFY_API_KEY to exist at process level.
    os.environ.setdefault("APIFY_API_KEY", "unused-db-backed-apify-token")
    os.environ.setdefault(
        "OPENAI_API_KEY",
        "unused-placeholder-twitter-solutions",
    )


def _to_twitter_scraper_tweet(item: dict, *, is_quote: bool = False):
    if item is None:
        return None

    from desearch.protocol import (
        TwitterScraperMedia,
        TwitterScraperTweet,
        TwitterScraperUser,
    )

    media_list = item.get("extendedEntities", {}).get("media", [])
    media_list = [
        TwitterScraperMedia(
            media_url=media.get("media_url_https"),
            type=media.get("type"),
        )
        for media in media_list
    ]

    author = item.get("author", {})
    quote = item.get("quoted_tweet")
    user = None

    if not is_quote:
        user = TwitterScraperUser(
            id=author.get("id"),
            created_at=author.get("createdAt"),
            description=author.get("description"),
            followers_count=author.get("followers"),
            favourites_count=author.get("favouritesCount"),
            listed_count=author.get("listedCount"),
            media_count=author.get("mediaCount"),
            statuses_count=author.get("statusesCount"),
            verified=author.get("isVerified"),
            is_blue_verified=author.get("isBlueVerified"),
            profile_image_url=author.get("profilePicture"),
            profile_banner_url=author.get("coverPicture") or None,
            url=author.get("url"),
            name=author.get("name"),
            username=author.get("userName"),
            entities=author.get("entities"),
            can_dm=author.get("canDm"),
            can_media_tag=author.get("canMediaTag"),
            location=author.get("location"),
            pinned_tweet_ids=author.get("pinnedTweetIds"),
        )

    return TwitterScraperTweet(
        id=item.get("id"),
        text=item.get("text"),
        reply_count=item.get("replyCount"),
        view_count=item.get("viewCount"),
        retweet_count=item.get("retweetCount"),
        like_count=item.get("likeCount"),
        quote_count=item.get("quoteCount"),
        bookmark_count=item.get("bookmarkCount"),
        url=item.get("url"),
        created_at=item.get("createdAt"),
        is_quote_tweet=item.get("isQuote"),
        is_retweet=item.get("isRetweet"),
        media=media_list,
        lang=item.get("lang"),
        conversation_id=item.get("conversationId"),
        quote=_to_twitter_scraper_tweet(quote, is_quote=True),
        entities=item.get("entities"),
        extended_entities=item.get("extendedEntities"),
        in_reply_to_status_id=item.get("inReplyToId"),
        quoted_status_id=quote.get("id") if quote else None,
        user=user,
    )


def _estimate_credit_usage(run_input: dict[str, Any]) -> float:
    max_items = run_input.get("maxItems")
    tweet_count = 0
    if max_items is not None:
        try:
            tweet_count = max(0, int(max_items))
        except (TypeError, ValueError):
            tweet_count = 0
    if tweet_count == 0:
        tweet_ids = run_input.get("tweetIDs")
        if isinstance(tweet_ids, list):
            tweet_count = len([t for t in tweet_ids if t])
    if tweet_count == 0:
        tweet_count = 1
    tweet_count = max(20, tweet_count)
    return (tweet_count / 1000.0) * _CREDIT_PER_1000_TWEETS


def _select_apify_account_token(usage_increment: float) -> tuple[int | None, str]:
    """
    Get an Apify token from DB where ``credit_amount + increment <= limit``.
    Returns ``(account_id, token)``.
    """
    try:
        from db.pg import connect, load_env

        load_env()
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, api_token
                    FROM public.apify_account
                    WHERE api_token IS NOT NULL
                      AND btrim(api_token) <> ''
                      AND (credit_amount + %s) <= %s
                    ORDER BY credit_amount ASC, id ASC
                    LIMIT 1
                    """,
                    (usage_increment, _APIFY_CREDIT_LIMIT),
                )
                row = cur.fetchone()
            conn.rollback()
        if row:
            return int(row[0]), str(row[1]).strip()
    except Exception:
        pass

    key = (os.environ.get("APIFY_API_KEY") or "").strip()
    if key:
        return None, key
    raise RuntimeError(
        "No usable Apify token found. Add rows in public.apify_account with "
        "api_token and credit_amount <= 5, or set APIFY_API_KEY as fallback."
    )


def _charge_apify_account(account_id: int | None, usage_increment: float) -> None:
    if account_id is None:
        return
    try:
        from db.pg import connect, load_env

        load_env()
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.apify_account
                    SET credit_amount = credit_amount + %s
                    WHERE id = %s
                    """,
                    (usage_increment, account_id),
                )
            conn.commit()
    except Exception:
        # Non-fatal: search result should still be returned.
        return


def _item_skip(item: dict) -> bool:
    return bool(
        item.get("noResults")
        or item.get("type") == "mock_tweet"
        or item.get("url") == ""
    )


def _normalize_lang(value: Any) -> str | None:
    lang = (str(value).strip().lower() if value is not None else "")
    return lang if lang in _ALLOWED_LANGS else None


async def run_actor_to_result_dicts(actor_id: str, run_input: dict) -> list[dict]:
    """Run an Apify actor and return ``TwitterScraperTweet``-shaped dicts."""
    usage_increment = _estimate_credit_usage(run_input)
    account_id, token = _select_apify_account_token(usage_increment)
    ensure_desearch_importable()
    from apify_client import ApifyClientAsync

    client = ApifyClientAsync(token=token)
    run = await client.actor(actor_id).call(run_input=run_input)
    _charge_apify_account(account_id, usage_increment)
    out: list[dict] = []
    async for item in client.dataset(run["defaultDatasetId"]).iterate_items():
        if _item_skip(item):
            continue
        try:
            tweet = _to_twitter_scraper_tweet(item)
            if tweet is not None:
                out.append(tweet.model_dump(mode="json"))
        except Exception:
            continue
    return out


def new_actor_search_input_from_synapse(synapse: Any) -> dict:
    """
    Map ``TwitterSearchSynapse`` fields to the new actor input schema:
    https://console.apify.com/actors/CJdippxWmn9uRfooo/information/latest0225/input
    """
    query = (getattr(synapse, "query", None) or "").strip()
    user = (getattr(synapse, "user", None) or "").strip()
    q_lower = query.lower()

    parts: list[str] = []
    if query:
        parts.append(query)
    if getattr(synapse, "verified", None) and "filter:verified" not in q_lower:
        parts.append("filter:verified")

    start = getattr(synapse, "start_date", None)
    end = getattr(synapse, "end_date", None)
    if start and "since:" not in q_lower:
        parts.append(f"since:{start}")
    if end and "until:" not in q_lower:
        parts.append(f"until:{end}")

    twitter_content = " ".join(parts).strip()
    if not twitter_content:
        twitter_content = query

    sort = getattr(synapse, "sort", None) or "Latest"
    if sort not in ("Top", "Latest"):
        sort = "Latest"

    run_input: dict[str, Any] = {
        "tweetIDs": [],
        "twitterContent": twitter_content,
        "searchTerms": [twitter_content] if twitter_content else [],
        "maxItems": max(1, int(getattr(synapse, "count", 20))),
        "queryType": sort,
        "from": user,
        "to": "",
        "@": "",
        "list": "",
        "filter:blue_verified": bool(getattr(synapse, "blue_verified", None)),
        "filter:quote": bool(getattr(synapse, "is_quote", None)),
        "filter:images": bool(getattr(synapse, "is_image", None)),
        "filter:videos": bool(getattr(synapse, "is_video", None)),
    }

    mr = getattr(synapse, "min_retweets", None)
    if mr is not None:
        run_input["min_retweets"] = int(mr)
    ml = getattr(synapse, "min_likes", None)
    if ml is not None:
        run_input["min_faves"] = int(ml)
    mre = getattr(synapse, "min_replies", None)
    if mre is not None:
        run_input["min_replies"] = int(mre)
    lang = _normalize_lang(getattr(synapse, "lang", None))
    if lang:
        run_input["lang"] = lang

    return run_input


def legacy_actor_search_input_from_synapse(synapse: Any) -> dict:
    """
    Map ``TwitterSearchSynapse`` to the legacy actor input:
    https://console.apify.com/actors/61RPP7dywgiy0JPD0/information/latest/input
    """
    query = (getattr(synapse, "query", None) or "").strip()
    user = getattr(synapse, "user", None)
    sort = getattr(synapse, "sort", None) or "Latest"
    lang = getattr(synapse, "lang", None) or "en"

    run_input: dict[str, Any] = {
        "searchTerms": [query] if query else [],
        "maxItems": max(1, int(getattr(synapse, "count", 20))),
        "sort": sort,
        "tweetLanguage": lang,
        "customMapFunction": "(object) => { return {...object} }",
    }
    if user:
        run_input["twitterHandles"] = [user]
    return run_input


def use_legacy_actor_from_env() -> bool:
    return os.environ.get("TWITTER_APIFY_ACTOR", "new").strip().lower() in (
        "legacy",
        "old",
        "61rpp7dywgiy0jpd0",
    )
