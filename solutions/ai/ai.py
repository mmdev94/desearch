"""
Fast AI-task solution orchestrator.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from openai import AsyncOpenAI

from desearch.dataset.date_filters import DateFilterType, get_specified_date_filter
from desearch.protocol import ResultType, ScraperTextRole, ScraperStreamingSynapse
from solutions.ai.arxiv_search import run_arxiv_search_sync
from solutions.ai.hacker_news import run_hn_algolia_search_sync
from solutions.ai.reddit_search import run_arctic_reddit_search_sync
from solutions.ai.wikipedia_api_search import run_wikipedia_search_sync
from solutions.ai.youtube_search_pkg import run_youtube_search_sync
from solutions.twitter.query import search as twitter_search
from solutions.web.search import run_web_search

_QUERY_MODEL = "gpt-4.1-nano"
_RANK_MODEL = "gpt-4.1-nano"
_SUMMARY_MODEL = "gpt-4.1-nano"

_DEFAULT_MAX_ITEMS = 20
_DEFAULT_PER_TOOL_ITEMS = 12
_PER_TOOL_TIMEOUT_SECONDS = 2.8
_OVERALL_TIMEOUT_SECONDS = 9.5

_TOOL_TO_KEY = {
    "Twitter Search": "twitter",
    "Web Search": "web",
    "Reddit Search": "reddit",
    "Youtube Search": "youtube",
    "YouTube Search": "youtube",
    "ArXiv Search": "arxiv",
    "Hacker News Search": "hackernews",
    "Wikipedia Search": "wikipedia",
}

_KEY_TO_TOOL = {
    "twitter": "Twitter Search",
    "web": "Web Search",
    "reddit": "Reddit Search",
    "youtube": "Youtube Search",
    "arxiv": "ArXiv Search",
    "hackernews": "Hacker News Search",
    "wikipedia": "Wikipedia Search",
}

_TOOL_WEIGHT = {
    "twitter": 1.35,
    "web": 1.15,
    "reddit": 1.0,
    "youtube": 1.0,
    "arxiv": 1.05,
    "hackernews": 1.0,
    "wikipedia": 0.9,
}


@dataclass
class UnifiedSource:
    tool_key: str
    title: str
    link: str
    snippet: str
    date: str | None = None

    def as_rank_payload(self, index: int) -> dict[str, Any]:
        return {
            "index": index,
            "tool": self.tool_key,
            "title": self.title,
            "link": self.link,
            "snippet": self.snippet[:700],
            "date": self.date,
        }


def _openai_client() -> AsyncOpenAI:
    api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for AI solution.")
    return AsyncOpenAI(api_key=api_key)


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _extract_query(task_query: str, task_meta: dict[str, Any]) -> str:
    ai_analysis = task_meta.get("ai_search_analysis") or {}
    rules = ai_analysis.get("tool_search_rules") or {}
    candidate_terms: list[str] = []
    for spec in rules.values():
        if isinstance(spec, dict):
            for q in spec.get("search_query_candidates", [])[:2]:
                if isinstance(q, str) and q.strip():
                    candidate_terms.append(q.strip())
    if candidate_terms:
        merged = ", ".join(candidate_terms[:4])
        return _normalize_whitespace(f"{task_query} {merged}")
    return _normalize_whitespace(task_query)


async def _plan_query_if_needed(task_query: str, task_meta: dict[str, Any]) -> str:
    simple = len(task_query.split()) <= 3
    if simple:
        return task_query.strip()

    base_hint = _extract_query(task_query, task_meta)
    client = _openai_client()
    system = (
        "Generate one concise search query string for multi-platform news/source retrieval. "
        "No quotes. Keep high recall with OR groups where useful. Return JSON only: "
        '{"query":"..."}'
    )
    user = f"task_query={task_query}\nanalysis_hint={base_hint}"
    try:
        resp = await client.chat.completions.create(
            model=_QUERY_MODEL,
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            timeout=2.5,
        )
        raw = resp.choices[0].message.content or "{}"
        payload = json.loads(raw)
        q = _normalize_whitespace(str(payload.get("query") or ""))
        return q.replace('"', "").replace("'", "") or base_hint
    except Exception:
        return base_hint


async def _run_twitter(query: str, count: int, date_filter_type: str | None) -> list[dict[str, Any]]:
    syn = SimpleNamespace(
        query=query,
        sort="Top",
        count=max(10, min(int(count), 50)),
        start_date=None,
        end_date=None,
        date_filter_type=date_filter_type,
        language="en",
    )
    out = await twitter_search(syn)
    return list(getattr(out, "results", []) or [])


async def _run_web(query: str, count: int) -> list[dict[str, Any]]:
    syn = SimpleNamespace(query=query, start=0, num=max(10, min(int(count), 50)))
    out = await run_web_search(syn)
    return list(getattr(out, "results", []) or [])


async def _run_thread(func, *args):
    return await asyncio.to_thread(func, *args)


def _run_reddit_sync(query: str, max_items: int) -> list[dict[str, str]]:
    return run_arctic_reddit_search_sync(query, max_items=max_items)


def _run_youtube_sync(query: str, max_items: int) -> list[dict[str, str]]:
    return run_youtube_search_sync(query, max_items=max_items)


def _run_arxiv_sync(query: str, max_items: int) -> list[dict[str, str]]:
    return run_arxiv_search_sync(query, max_items=max_items)


def _run_hn_sync(query: str, max_items: int) -> list[dict[str, str]]:
    return run_hn_algolia_search_sync(query, max_items=max_items)


def _run_wikipedia_sync(query: str, max_items: int) -> list[dict[str, str]]:
    return run_wikipedia_search_sync(query, max_items=max_items)


def _dedupe_sources(rows: list[UnifiedSource]) -> list[UnifiedSource]:
    seen: set[str] = set()
    out: list[UnifiedSource] = []
    for r in rows:
        link = (r.link or "").strip().lower().rstrip("/")
        if not link or link in seen:
            continue
        seen.add(link)
        out.append(r)
    return out


def _collect_source_rows(
    by_tool: dict[str, list[dict[str, Any]]],
) -> list[UnifiedSource]:
    rows: list[UnifiedSource] = []
    for tool_key, items in by_tool.items():
        for item in items:
            if tool_key == "twitter":
                user = item.get("user") or {}
                username = str(user.get("username") or "").strip() or "i"
                tid = str(item.get("id") or "").strip()
                link = (item.get("url") or f"https://x.com/{username}/status/{tid}").strip()
                title = (item.get("text") or "")[:100]
                snippet = _normalize_whitespace(item.get("text") or "")
                rows.append(
                    UnifiedSource(
                        tool_key=tool_key,
                        title=title or f"Tweet by @{username}",
                        link=link,
                        snippet=snippet,
                        date=item.get("created_at"),
                    )
                )
            else:
                title = _normalize_whitespace(str(item.get("title") or ""))
                link = _normalize_whitespace(str(item.get("link") or ""))
                snippet = _normalize_whitespace(str(item.get("snippet") or title))
                rows.append(
                    UnifiedSource(
                        tool_key=tool_key,
                        title=title or link[:80],
                        link=link,
                        snippet=snippet,
                        date=item.get("date"),
                    )
                )
    return _dedupe_sources(rows)


def _keyword_overlap_score(query: str, text: str) -> float:
    q_tokens = {t for t in re.findall(r"[a-z0-9]{3,}", query.lower())}
    if not q_tokens:
        return 0.0
    s_tokens = set(re.findall(r"[a-z0-9]{3,}", text.lower()))
    inter = len(q_tokens.intersection(s_tokens))
    return inter / max(1, len(q_tokens))


async def _rank_sources_with_llm(
    query: str,
    date_filter_type: str | None,
    rows: list[UnifiedSource],
    max_items: int,
) -> list[UnifiedSource]:
    if not rows:
        return []

    client = _openai_client()
    payload = [r.as_rank_payload(i + 1) for i, r in enumerate(rows[:80])]
    system = (
        "Score each source for relevance to query and date intent. "
        "Twitter and Web should be preferred when scores are close. "
        "Return JSON object only: {\"scores\":[{\"index\":1,\"score\":0-100,\"reason\":\"...\"}]}"
    )
    user = json.dumps(
        {
            "query": query,
            "date_filter_type": date_filter_type,
            "top_n": max_items,
            "sources": payload,
        },
        ensure_ascii=False,
    )
    score_map: dict[int, float] = {}
    try:
        resp = await client.chat.completions.create(
            model=_RANK_MODEL,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            timeout=3.0,
        )
        raw = resp.choices[0].message.content or "{}"
        parsed = json.loads(raw)
        for item in parsed.get("scores", []):
            idx = int(item.get("index"))
            score = float(item.get("score", 0))
            if idx > 0:
                score_map[idx] = max(0.0, min(100.0, score))
    except Exception:
        score_map = {}

    ranked: list[tuple[float, UnifiedSource]] = []
    for i, row in enumerate(rows, start=1):
        llm_score = score_map.get(i, 50.0)
        base = _keyword_overlap_score(query, f"{row.title} {row.snippet}") * 100.0
        tool_bonus = (_TOOL_WEIGHT.get(row.tool_key, 1.0) - 1.0) * 25.0
        ranked.append((llm_score * 0.65 + base * 0.35 + tool_bonus, row))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in ranked[:max_items]]


async def _build_summary(
    query: str,
    date_filter_type: str | None,
    top_rows: list[UnifiedSource],
) -> str:
    if not top_rows:
        return (
            "**Findings**\nNo relevant sources were found.\n\n"
            "**Conclusion**\nThe query needs broader terms or updated data."
        )
    client = _openai_client()
    data = [
        {
            "index": i,
            "tool": s.tool_key,
            "title": s.title,
            "link": s.link,
            "snippet": s.snippet[:700],
            "date": s.date,
        }
        for i, s in enumerate(top_rows, 1)
    ]
    system = (
        "Write markdown answer with **bold section headers** only (no # headers). "
        "Use 3-4 sections, last section **Conclusion**. "
        "Support claims with inline markdown links using provided urls, e.g. [1](url). "
        "No standalone sources section. Max 400 words."
    )
    user = json.dumps(
        {
            "question": query,
            "date_filter_type": date_filter_type,
            "sources": data,
        },
        ensure_ascii=False,
    )
    try:
        resp = await client.chat.completions.create(
            model=_SUMMARY_MODEL,
            temperature=0.35,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            timeout=3.2,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text or "**Conclusion**\nInsufficient content for summary."
    except Exception:
        return (
            "**Key Updates**\n"
            + "\n".join(f"- [{i}]({s.link}) {s.title}" for i, s in enumerate(top_rows[:8], 1))
            + "\n\n**Conclusion**\nThe selected sources indicate the main recent developments."
        )


async def run_ai_solution(
    synapse: ScraperStreamingSynapse,
    task_meta: dict[str, Any] | None = None,
) -> ScraperStreamingSynapse:
    start = time.perf_counter()
    task_meta = task_meta or {}

    prompt = _normalize_whitespace(synapse.prompt)
    planned_query = await _plan_query_if_needed(prompt, task_meta)
    max_items = int(getattr(synapse, "count", None) or _DEFAULT_MAX_ITEMS)
    per_tool_items = max(6, min(_DEFAULT_PER_TOOL_ITEMS, max_items))

    tools = list(synapse.tools or []) or list(_TOOL_TO_KEY.keys())
    enabled_keys = []
    for t in tools:
        key = _TOOL_TO_KEY.get(t)
        if key and key not in enabled_keys:
            enabled_keys.append(key)

    async def _bounded(coro):
        return await asyncio.wait_for(coro, timeout=_PER_TOOL_TIMEOUT_SECONDS)

    tasks: dict[str, asyncio.Task] = {}
    if "twitter" in enabled_keys:
        tasks["twitter"] = asyncio.create_task(
            _bounded(_run_twitter(planned_query, per_tool_items, synapse.date_filter_type))
        )
    if "web" in enabled_keys:
        tasks["web"] = asyncio.create_task(_bounded(_run_web(planned_query, per_tool_items)))
    if "reddit" in enabled_keys:
        tasks["reddit"] = asyncio.create_task(
            _bounded(_run_thread(_run_reddit_sync, planned_query, per_tool_items))
        )
    if "youtube" in enabled_keys:
        tasks["youtube"] = asyncio.create_task(
            _bounded(_run_thread(_run_youtube_sync, planned_query, per_tool_items))
        )
    if "arxiv" in enabled_keys:
        tasks["arxiv"] = asyncio.create_task(
            _bounded(_run_thread(_run_arxiv_sync, planned_query, per_tool_items))
        )
    if "hackernews" in enabled_keys:
        tasks["hackernews"] = asyncio.create_task(
            _bounded(_run_thread(_run_hn_sync, planned_query, per_tool_items))
        )
    if "wikipedia" in enabled_keys:
        tasks["wikipedia"] = asyncio.create_task(
            _bounded(_run_thread(_run_wikipedia_sync, planned_query, per_tool_items))
        )

    by_tool: dict[str, list[dict[str, Any]]] = {}
    wait_deadline = _OVERALL_TIMEOUT_SECONDS
    done, pending = await asyncio.wait(tasks.values(), timeout=wait_deadline)
    for p in pending:
        p.cancel()
    for key, task in tasks.items():
        if task in done and not task.cancelled():
            try:
                by_tool[key] = list(task.result() or [])
            except Exception:
                by_tool[key] = []
        else:
            by_tool[key] = []

    rows = _collect_source_rows(by_tool)
    top_rows = await _rank_sources_with_llm(
        planned_query,
        synapse.date_filter_type,
        rows,
        max_items=max_items,
    )
    summary = await _build_summary(planned_query, synapse.date_filter_type, top_rows)

    synapse.miner_tweets = by_tool.get("twitter", [])
    synapse.search_results = by_tool.get("web", [])
    synapse.reddit_search_results = by_tool.get("reddit", [])
    synapse.youtube_search_results = by_tool.get("youtube", [])
    synapse.arxiv_search_results = by_tool.get("arxiv", [])
    synapse.hacker_news_search_results = by_tool.get("hackernews", [])
    synapse.wikipedia_search_results = by_tool.get("wikipedia", [])
    synapse.result_type = ResultType.LINKS_WITH_FINAL_SUMMARY
    synapse.completion = "completed"
    synapse.text_chunks = synapse.text_chunks or {}
    synapse.text_chunks[ScraperTextRole.FINAL_SUMMARY.value] = [summary]
    synapse.dendrite = {"status_code": 200, "process_time": time.perf_counter() - start}
    return synapse


def build_ai_synapse_from_task(task: dict[str, Any]) -> ScraperStreamingSynapse:
    q = task.get("query") or {}
    prompt = _normalize_whitespace(str(q.get("query") or ""))
    tools = q.get("tools") or list(_TOOL_TO_KEY.keys())
    date_filter_type = q.get("date_filter_type") or DateFilterType.PAST_WEEK.value
    date_filter = get_specified_date_filter(DateFilterType(date_filter_type))
    return ScraperStreamingSynapse(
        prompt=prompt,
        tools=tools,
        result_type=ResultType.LINKS_WITH_FINAL_SUMMARY,
        count=int(q.get("count") or _DEFAULT_MAX_ITEMS),
        max_execution_time=int(q.get("max_execution_time") or 10),
        start_date=date_filter.start_date.strftime("%Y-%m-%dT%H:%M:%SZ")
        if date_filter.start_date
        else None,
        end_date=date_filter.end_date.strftime("%Y-%m-%dT%H:%M:%SZ")
        if date_filter.end_date
        else None,
        date_filter_type=date_filter_type,
    )
