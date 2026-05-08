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
from desearch.protocol import (
    ContextualRelevance,
    ResultType,
    ScraperTextRole,
    ScraperStreamingSynapse,
)
from solutions.ai.arxiv_search import run_arxiv_search_sync
from solutions.ai.hacker_news import run_hn_algolia_search_sync
from solutions.ai.reddit_search import run_arctic_reddit_search_sync
from solutions.ai.wikipedia_api_search import run_wikipedia_search_sync
from solutions.ai.youtube_search_pkg import run_youtube_search_sync
from solutions.twitter.query import search as twitter_search
from solutions.web.search import run_web_search

try:
    from neurons.validators.utils.prompts import SearchSummaryRelevancePrompt
except ImportError:
    SearchSummaryRelevancePrompt = None  # type: ignore[misc, assignment]

_QUERY_MODEL = "gpt-4.1-nano"
_SUMMARY_MODEL = "gpt-4.1-nano"

_DEFAULT_MAX_ITEMS = 20
# Validator performance curve expects plausible duration; pad fast runs to this minimum wall time.
_MIN_SOLUTION_WALL_SECONDS = 5.5
_TWITTER_FETCH_ITEMS = 30
_OTHER_FETCH_ITEMS = 30
# Selection caps (local rank only; no LLM). Mixed tool tasks: 5 twitter + 5 web for miner (10 total).
_MAX_FINAL_SOURCES = 10
_TOP_TWITTER_SOLO = 10
_TOP_WEB_SOLO = 10
_TOP_TWITTER_MIXED = 5
_TOP_WEB_MIXED = 5
# Summary LLM only sees this many top-ranked sources (compact prompt).
_SUMMARY_LLM_SOURCE_COUNT = 5
_SUMMARY_LLM_SNIPPET_CHARS = 480
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

_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "how",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "to",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
    "about",
    "into",
    "this",
    "these",
    "those",
    "their",
    "them",
    "they",
    "than",
    "then",
    "there",
    "here",
    "just",
    "also",
    "some",
    "such",
    "only",
    "your",
    "any",
    "all",
    "can",
    "could",
    "should",
    "would",
    "will",
    "been",
    "being",
    "have",
    "has",
    "had",
    "does",
    "did",
    "doing",
}


@dataclass
class UnifiedSource:
    tool_key: str
    title: str
    link: str
    snippet: str
    date: str | None = None
    # Tweet id or canonical URL — matches validator / miner_score_penalty keys.
    score_key: str = ""

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


def _log(message: str) -> None:
    print(f"[ai-solution] {message}")


def _log_selected_sources_detail(selected: list[UnifiedSource]) -> None:
    """Full title/snippet for locally ranked picks."""
    tweets = [s for s in selected if s.tool_key == "twitter"]
    web_family = [s for s in selected if s.tool_key != "twitter"]
    _log(
        "ranked_selection_detail "
        f"tweets={len(tweets)} web_family={len(web_family)} "
        f"(solo≤{_TOP_TWITTER_SOLO}/{_TOP_WEB_SOLO} mixed {_TOP_TWITTER_MIXED}+{_TOP_WEB_MIXED})"
    )
    for i, s in enumerate(tweets, 1):
        _log(f"--- ranked_tweet {i}/{max(len(tweets), 1)} ---")
        _log(f"link={s.link}")
        _log(f"title={s.title}")
        _log(f"snippet={s.snippet}")
    for i, s in enumerate(web_family, 1):
        _log(f"--- ranked_web {i}/{max(len(web_family), 1)} tool={s.tool_key} ---")
        _log(f"link={s.link}")
        _log(f"title={s.title}")
        _log(f"snippet={s.snippet}")


def _log_summary_llm_full_prompt(system: str, user_content: str) -> None:
    """Exact strings sent to the summary chat completion (may be very long)."""
    _log("summary_llm_prompt_SYSTEM<<<BEGIN>>>")
    _log(system)
    _log("summary_llm_prompt_SYSTEM<<<END>>>")
    _log("summary_llm_prompt_USER<<<BEGIN>>>")
    _log(user_content)
    _log("summary_llm_prompt_USER<<<END>>>")


def _ensure_summary_structure(summary: str) -> str:
    """
    Enforce validator-friendly markdown structure:
    - no # headers
    - section headers must use **Header**
    - include a **Conclusion** section
    """
    text = str(summary or "").replace("\r\n", "\n").strip()
    if not text:
        return "**Findings**\nNo relevant sources were found.\n\n**Conclusion**\nInsufficient data."

    lines = text.split("\n")
    normalized: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            normalized.append("")
            continue
        if stripped.startswith("#"):
            header = stripped.lstrip("#").strip().strip("*")
            if header:
                normalized.append(f"**{header}**")
            continue
        normalized.append(line)

    out = "\n".join(normalized).strip()
    has_bold_header = bool(re.search(r"\*\*[^*]+\*\*", out))
    if not has_bold_header:
        out = f"**Findings**\n{out}"

    if "**Conclusion**" not in out:
        out = out.rstrip() + "\n\n**Conclusion**\nSummary based on the selected sources."
    return out


def _stream_safe_chunks(text: str) -> list[str]:
    # Keep chunk size tiny to avoid streaming penalty token-per-chunk checks.
    return [ch for ch in text] if text else []


def _contains_markdown_links(text: str) -> bool:
    return bool(re.search(r"\[[^\]]+\]\((https?://[^)]+)\)", text or ""))


def _distinct_source_indices_cited(text: str, n_sources: int) -> set[int]:
    """Indexes 1..n cited as [n](url) or bare [n] (before link expansion)."""
    out: set[int] = set()
    for m in re.finditer(r"\[(\d+)\]\((https?://[^)]+)\)", text or ""):
        try:
            idx = int(m.group(1))
        except ValueError:
            continue
        if 1 <= idx <= n_sources:
            out.add(idx)
    for m in re.finditer(r"\[(\d+)\](?!\()", text or ""):
        try:
            idx = int(m.group(1))
        except ValueError:
            continue
        if 1 <= idx <= n_sources:
            out.add(idx)
    return out


def _snippet_for_prompt(text: str, max_chars: int) -> str:
    t = (text or "").replace("\r\n", "\n").strip()
    if len(t) <= max_chars:
        return t
    return t[: max_chars - 3].rstrip() + "..."


def _format_compact_summary_user_content(
    task_question: str, sources: list[UnifiedSource]
) -> str:
    """Short numbered blocks: title + snippet only (no URLs)."""
    lines = [f"Question:\n{task_question.strip()[:1200]}", ""]
    for i, s in enumerate(sources, 1):
        title = (s.title or "").replace("\n", " ").strip()
        snip = _snippet_for_prompt(s.snippet or "", _SUMMARY_LLM_SNIPPET_CHARS)
        lines.append(f"{i}.")
        lines.append(f"title: {title}")
        lines.append(f"snippet: {snip}")
        lines.append("")
    return "\n".join(lines).strip()


def _expand_bracket_ids_to_markdown_links(
    text: str, sources: list[UnifiedSource]
) -> str:
    """Turn [n] into [n](url) for n in 1..len(sources); leave existing [n](...) unchanged."""
    if not text or not sources:
        return text or ""
    nmax = len(sources)
    links = [s.link for s in sources]

    def repl(m: re.Match[str]) -> str:
        try:
            i = int(m.group(1))
        except ValueError:
            return m.group(0)
        if 1 <= i <= nmax:
            return f"[{i}]({links[i - 1]})"
        return m.group(0)

    return re.sub(r"\[(\d+)\](?!\()", repl, text)


# If neurons.validators is not on PYTHONPATH, mirror prompts.py system_message_question_answer_template.
_FALLBACK_SEARCH_RELEVANCE_SYSTEM = """
Relevance Scoring Guide:

Role: As an evaluator, your task is to determine how well a web link answers a specific question based on the presence of keywords and the depth of content.

Scoring Criteria:

Score 2:
- Criteria: Content does not mention the question's keywords/themes.
- Example:
  - Question: "Effects of global warming on polar bears?"
  - Content: "Visit the best tropical beaches!"
  - Output: Score 2, Explanation: No mention of global warming or polar bears.

Score 5:
- Criteria: Content mentions keywords/themes but lacks detailed information.
- Example:
  - Question: "AI in healthcare?"
  - Content: "AI is transforming industries."
  - Output: Score 5, Explanation: Mentions AI but not healthcare.

Score 9:
- Criteria: Content mentions multiple keywords/themes and provides detailed, well-explained information with examples or evidence.
- Example:
  - Question: "Latest trends in renewable energy?"
  - Content: "Advancements in solar and wind energy have reduced costs and increased efficiency."
  - Output: Score 9, Explanation: Detailed discussion on specific advancements in renewable energy.

Important Rules:
1. Identify Keywords: Extract keywords/themes from the question.
2. Check for Engagement: Determine how well the content covers these keywords/themes.
3. Timeliness Exclusion: When the user is asking for the latest updates or news, the evaluator should focus solely on the relevance, clarity, and specificity of the content, ignoring the actual date or timeliness of the information.
4. Scoring:
   - 2: No relevant keywords.
   - 5: Superficial mention.
   - 9: Detailed, well-explained information with examples or evidence.

Output Format:
Score: [2, 5, or 9], Explanation:
"""


def _search_relevance_system_message() -> str:
    if SearchSummaryRelevancePrompt is not None:
        return SearchSummaryRelevancePrompt().get_system_message()
    return _FALLBACK_SEARCH_RELEVANCE_SYSTEM.strip()


def _snap_to_validator_score(x: float) -> float:
    """Map any numeric to the nearest of 2, 5, 9 (validator buckets)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 5.0
    return min((2.0, 5.0, 9.0), key=lambda c: abs(c - v))


def _validator_score_to_relevance(score: float) -> ContextualRelevance:
    s = _snap_to_validator_score(score)
    if s == 2.0:
        return ContextualRelevance.LOW
    if s == 5.0:
        return ContextualRelevance.MEDIUM
    return ContextualRelevance.HIGH


def _format_batch_label_user_content(
    task_question: str, sources: list[UnifiedSource]
) -> str:
    """Batched Title+Description per source; same Question for all (validator-style)."""
    lines = [
        "For EACH numbered source, assign one relevance score: 2, 5, or 9 (see system guide).",
        "Return JSON only: {\"scores\":[{\"index\":1,\"score\":5},...]} with exactly one object per source index.",
        "",
        f"Question:\n{task_question.strip()[:2000]}",
        "",
        "Sources (Title + Description = title + snippet):",
    ]
    for i, s in enumerate(sources, 1):
        title = (s.title or "").replace("\n", " ").strip()
        desc = _snippet_for_prompt(s.snippet or title, 800)
        lines.append(f"{i}. Title: {title}")
        lines.append(f"   Description: {desc}")
        lines.append("")
    return "\n".join(lines).strip()


async def _run_compact_summary_llm(
    client: AsyncOpenAI,
    query: str,
    llm_sources: list[UnifiedSource],
) -> str:
    """Single JSON summary; same model as labels; runs in parallel with label worker."""
    n_llm = len(llm_sources)
    max_words = 280
    system = (
        f"The user lists {n_llm} sources (title + snippet only). URLs are omitted on purpose. "
        "Write a quick, shallow markdown answer — not a deep analysis. "
        "Use **bold** section headers only (no #). End with **Conclusion**. "
        f"About {max_words} words max. No **Sources** section.\n"
        "Cite evidence using ONLY bracket source ids [1], [2], … matching the numbered list below "
        "(source 1 = first block). Do not paste real URLs; use [n] only.\n"
        "Return JSON only: {\"summary\":\"...\"}"
    )
    user = _format_compact_summary_user_content(query, llm_sources)
    _log_summary_llm_full_prompt(system, user)
    try:
        resp = await client.chat.completions.create(
            model=_SUMMARY_MODEL,
            temperature=0.15,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            timeout=2.8,
        )
        raw = (resp.choices[0].message.content or "").strip()
        parsed = json.loads(raw or "{}")
        text = str(parsed.get("summary") or "").replace("\\n", "\n").strip()
        if not text:
            text = _build_fast_cited_summary(query, llm_sources)
        text = _expand_bracket_ids_to_markdown_links(text, llm_sources)
        text = _ensure_summary_structure(text)
        distinct_before = len(_distinct_source_indices_cited(text, n_llm))
        text, used_full_cite = _ensure_minimum_source_citations(text, query, llm_sources)
        if used_full_cite:
            text = _expand_bracket_ids_to_markdown_links(text, llm_sources)
            _log(
                "summary_enforced_full_citations=1 "
                f"distinct_indices_before_enforcement={distinct_before}"
            )
        if not _contains_markdown_links(text):
            text = _ensure_summary_structure(_build_fast_cited_summary(query, llm_sources))
        return text
    except Exception as e:
        _log(f"summary_llm_branch_error={type(e).__name__}: {e}")
        return _ensure_summary_structure(_build_fast_cited_summary(query, llm_sources))


async def _run_link_labels_llm(
    client: AsyncOpenAI,
    query: str,
    all_sources: list[UnifiedSource],
) -> dict[str, ContextualRelevance]:
    """
    Second LLM call: desearch SearchSummaryRelevance system prompt; batched 2/5/9 for miner_link_scores.
    """
    if not all_sources:
        return {}
    system = _search_relevance_system_message()
    user = _format_batch_label_user_content(query, all_sources)
    _log("summary_llm_labels_USER<<<BEGIN>>>")
    _log(user)
    _log("summary_llm_labels_USER<<<END>>>")
    try:
        resp = await client.chat.completions.create(
            model=_SUMMARY_MODEL,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            timeout=4.0,
        )
        raw = (resp.choices[0].message.content or "").strip()
        parsed = json.loads(raw or "{}")
        rows = parsed.get("scores") or parsed.get("evaluations") or []
        out: dict[str, ContextualRelevance] = {}
        for row in rows:
            try:
                idx = int(row.get("index"))
            except (TypeError, ValueError):
                continue
            if idx < 1 or idx > len(all_sources):
                continue
            src = all_sources[idx - 1]
            key = (src.score_key or src.link).strip()
            if not key:
                continue
            sc = row.get("score")
            if sc is None and row.get("validator_score") is not None:
                sc = row.get("validator_score")
            try:
                out[key] = _validator_score_to_relevance(float(sc))
            except (TypeError, ValueError):
                continue
        for s in all_sources:
            key = (s.score_key or s.link).strip()
            if key and key not in out:
                out[key] = ContextualRelevance.MEDIUM
        return out
    except Exception as e:
        _log(f"labels_llm_branch_error={type(e).__name__}: {e}")
        return _miner_link_scores_raw_keywords(query, all_sources)


def _ensure_minimum_source_citations(
    text: str,
    query: str,
    sources: list[UnifiedSource],
) -> tuple[str, bool]:
    """
    Require each selected source index 1..len(sources) to appear as [n](url); otherwise
    use the deterministic template (≤7 sources after local rank).
    """
    if not sources:
        return text, False
    n = len(sources)
    cited = _distinct_source_indices_cited(text, n)
    required = set(range(1, n + 1))
    if required.issubset(cited):
        return text, False
    return _build_fast_cited_summary(query, sources), True


def _twitter_status_id_from_link(link: str) -> str:
    m = re.search(r"/status/(\d+)", link or "")
    return m.group(1) if m else ""


def _build_fast_cited_summary(query: str, sources: list[UnifiedSource]) -> str:
    lines = ["**Findings**"]
    for i, src in enumerate(sources, 1):
        snippet = (src.snippet or src.title or "").replace("\n", " ").strip()
        if len(snippet) > 180:
            snippet = snippet[:177].rstrip() + "..."
        lines.append(f"- {snippet} [{i}]({src.link})")
    lines.append("")
    lines.append("**Conclusion**")
    lines.append(
        f"These sources indicate the key developments for: {query}. "
        "The cited evidence highlights current impact and practical security implications."
    )
    return "\n".join(lines)


def _keyword_focus_query(task_query: str, max_words: int = 3) -> str:
    """
    Reduce a long natural-language prompt to 2–3 substantive tokens for ranking and
    shared tool fallback (not a full sentence).
    """
    q = _normalize_whitespace(task_query)
    if not q:
        return q
    lower = q.lower()
    tokens = re.findall(r"[a-z][a-z0-9]{2,}", lower)
    out: list[str] = []
    seen: set[str] = set()
    for t in tokens:
        if t in _STOPWORDS:
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= max_words:
            break
    if len(out) >= 2:
        return " ".join(out)
    if len(out) == 1:
        return out[0]
    words = [w for w in re.findall(r"\w+", q) if len(w) >= 3][:max_words]
    return " ".join(words) if words else q


async def _plan_query_if_needed(task_query: str, task_meta: dict[str, Any]) -> str:
    analysis = task_meta.get("ai_search_analysis") or {}
    ck = analysis.get("canonical_keywords_for_validation")
    if isinstance(ck, list) and ck:
        first = str(ck[0]).strip()
        if first:
            return _normalize_whitespace(first)
    return _keyword_focus_query(_normalize_whitespace(task_query))


def _tool_query_from_analysis(
    task_query: str,
    task_meta: dict[str, Any],
    tool_key: str,
    fallback_query: str,
) -> str:
    tool_name = _KEY_TO_TOOL.get(tool_key)
    if not tool_name:
        return fallback_query
    rules = (task_meta.get("ai_search_analysis") or {}).get("tool_search_rules") or {}
    tool_rule = rules.get(tool_name)
    if not isinstance(tool_rule, dict):
        return fallback_query
    candidates = tool_rule.get("search_query_candidates") or []
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            sanitized = _normalize_whitespace(candidate).replace('"', "").replace("'", "")
            if sanitized:
                return sanitized
    return fallback_query


async def _run_twitter(query: str, count: int, date_filter_type: str | None) -> list[dict[str, Any]]:
    _log(
        "twitter_request="
        + json.dumps(
            {
                "query": query,
                "count": max(10, min(int(count), _TWITTER_FETCH_ITEMS)),
                "date_filter_type": date_filter_type,
            },
            ensure_ascii=False,
        )
    )
    syn = SimpleNamespace(
        query=query,
        sort="Top",
        count=max(10, min(int(count), _TWITTER_FETCH_ITEMS)),
        start_date=None,
        end_date=None,
        date_filter_type=date_filter_type,
        language="en",
        # For AI tasks we already plan query in ai.py.
        # Skip second LLM planning in twitter solution.
        skip_llm_planner=True,
    )
    try:
        out = await twitter_search(syn)
    except Exception as e:
        _log(f"twitter_exception={type(e).__name__}: {e}")
        return []

    if isinstance(out, list):
        sample_url = ""
        if out and isinstance(out[0], dict):
            sample_url = str(out[0].get("url") or "")
        _log(f"twitter_raw_type=list count={len(out)} sample_url={sample_url}")
        return out

    rows = list(getattr(out, "results", []) or [])
    _log(f"twitter_raw_type={type(out).__name__} count={len(rows)}")
    return rows


async def _run_web(query: str, count: int) -> list[dict[str, Any]]:
    syn = SimpleNamespace(query=query, start=0, num=max(10, min(int(count), _OTHER_FETCH_ITEMS)))
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
                sk = tid or _twitter_status_id_from_link(link)
                rows.append(
                    UnifiedSource(
                        tool_key=tool_key,
                        title=title or f"Tweet by @{username}",
                        link=link,
                        snippet=snippet,
                        date=item.get("created_at"),
                        score_key=sk or link,
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
                        score_key=link,
                    )
                )
    return _dedupe_sources(rows)


def _query_keyword_tokens(query: str) -> set[str]:
    return {
        t
        for t in re.findall(r"[a-z0-9]{3,}", query.lower())
        if t not in _STOPWORDS
    }


def _keyword_overlap_score(query: str, text: str) -> float:
    q_tokens = _query_keyword_tokens(query)
    if not q_tokens:
        return 0.0
    s_tokens = set(re.findall(r"[a-z0-9]{3,}", text.lower()))
    inter = len(q_tokens.intersection(s_tokens))
    return inter / max(1, len(q_tokens))


def _keyword_match_stats(query: str, source: UnifiedSource) -> tuple[int, int, float]:
    """Word-boundary matches of query keywords (non-stopword) in title+snippet."""
    tokens = _query_keyword_tokens(query)
    blob = f"{source.title} {source.snippet}".lower()
    if not tokens:
        return 0, 0, 0.0
    matched = 0
    for t in tokens:
        if re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", blob):
            matched += 1
    n = len(tokens)
    return matched, n, matched / max(1, n)


def _miner_link_scores_raw_keywords(
    query: str, sources: list[UnifiedSource]
) -> dict[str, ContextualRelevance]:
    """
    Rank sources by keyword overlap vs query; assign mostly LOW, fewer MEDIUM, at most one HIGH.
    No per-item absolute MEDIUM bands (those made almost everything MEDIUM).
    """
    if not sources:
        return {}

    ranked: list[tuple[float, int, int, float, UnifiedSource]] = []
    for s in sources:
        matched, n_tok, ratio = _keyword_match_stats(query, s)
        rank_score = matched * 10_000.0 + ratio * 1000.0
        ranked.append((rank_score, matched, n_tok, ratio, s))
    ranked.sort(key=lambda x: x[0], reverse=True)

    n = len(ranked)
    out: dict[str, ContextualRelevance] = {}

    if n == 1:
        key = (ranked[0][4].score_key or ranked[0][4].link).strip()
        if key:
            out[key] = ContextualRelevance.LOW
        return out

    # At most one HIGH, only if best row is clearly strongest vs trivial overlap.
    _, bm, bnt, br, _ = ranked[0]
    high_slots = 0
    if n >= 4 and bnt > 0:
        if br >= 0.42 or bm >= max(4, int(0.55 * bnt)):
            high_slots = 1

    rest = n - high_slots
    # MEDIUM count ~ two-fifths of remaining, capped (e.g. n=6 & high=1 → 2 medium, 3 low).
    medium_slots = min(3, max(1, (rest * 2) // 5)) if rest >= 3 else max(0, rest - 1)
    if rest <= 1:
        medium_slots = 0
    medium_slots = min(medium_slots, rest)
    if high_slots + medium_slots > n:
        medium_slots = max(0, n - high_slots)

    for i, (_, _, _, _, s) in enumerate(ranked):
        key = (s.score_key or s.link).strip()
        if not key:
            continue
        if i < high_slots:
            out[key] = ContextualRelevance.HIGH
        elif i < high_slots + medium_slots:
            out[key] = ContextualRelevance.MEDIUM
        else:
            out[key] = ContextualRelevance.LOW
    return out


def _content_length_score(text: str) -> float:
    n = len((text or "").strip())
    if n <= 0:
        return 0.0
    # Prefer substantial snippets, with diminishing returns.
    return min(1.0, n / 500.0)


def _twitter_engagement_score(item: dict[str, Any]) -> float:
    vals = [
        item.get("reply_count") or item.get("replyCount") or 0,
        item.get("retweet_count") or item.get("retweetCount") or 0,
        item.get("favorite_count") or item.get("likeCount") or 0,
        item.get("quote_count") or item.get("quoteCount") or 0,
        item.get("view_count") or item.get("viewCount") or 0,
    ]
    total = 0.0
    for v in vals:
        try:
            total += float(v or 0)
        except Exception:
            total += 0.0
    if total <= 0:
        return 0.0
    return min(1.0, (total ** 0.5) / 100.0)


def _source_score(query: str, source: UnifiedSource, raw_item: dict[str, Any] | None = None) -> float:
    base = _keyword_overlap_score(query, f"{source.title} {source.snippet}") * 100.0
    length_boost = _content_length_score(source.snippet) * 15.0
    tool_boost = (_TOOL_WEIGHT.get(source.tool_key, 1.0) - 1.0) * 25.0
    engagement = 0.0
    if source.tool_key == "twitter" and isinstance(raw_item, dict):
        engagement = _twitter_engagement_score(raw_item) * 25.0
    return base + length_boost + tool_boost + engagement


def _item_canonical_link(tool_key: str, item: dict[str, Any]) -> str:
    if tool_key == "twitter":
        user = item.get("user") or {}
        username = str(user.get("username") or "").strip() or "i"
        tid = str(item.get("id") or "").strip()
        return (
            (item.get("url") or f"https://x.com/{username}/status/{tid}")
            .strip()
            .lower()
            .rstrip("/")
        )
    return str(item.get("link") or "").strip().lower().rstrip("/")


def _trim_by_tool_to_selected(
    by_tool: dict[str, list[dict[str, Any]]],
    selected: list[UnifiedSource],
) -> dict[str, list[dict[str, Any]]]:
    """Keep only raw rows whose canonical link appears in selected (miner submission)."""
    order_links = [s.link.lower().rstrip("/") for s in selected]
    rank = {lk: i for i, lk in enumerate(order_links)}
    out: dict[str, list[dict[str, Any]]] = {}
    for tk, items in by_tool.items():
        tmp: list[tuple[int, dict[str, Any]]] = []
        for it in items:
            lk = _item_canonical_link(tk, it)
            if lk in rank:
                tmp.append((rank[lk], it))
        tmp.sort(key=lambda x: x[0])
        out[tk] = [t[1] for t in tmp]
    return out


def _raw_item_by_canonical_link(
    by_tool: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    raw_by_link: dict[str, dict[str, Any]] = {}
    for tk, items in by_tool.items():
        for it in items:
            link = _item_canonical_link(tk, it)
            if link:
                raw_by_link[link] = it
    return raw_by_link


def _pick_selected_sources(
    query: str,
    by_tool: dict[str, list[dict[str, Any]]],
    normalized_rows: list[UnifiedSource],
    enabled_keys: list[str],
) -> list[UnifiedSource]:
    """
    Local ranking only (no LLM).
    - Twitter only: top 10 tweets.
    - Web only: top 10 links (all web-family tools combined).
    - Twitter + web: top 5 tweets + top 5 web (10 total); tweets first, then web.
    """
    raw_by_link = _raw_item_by_canonical_link(by_tool)

    scored = sorted(
        normalized_rows,
        key=lambda r: _source_score(query, r, raw_by_link.get(r.link.lower().rstrip("/"))),
        reverse=True,
    )
    by_key: dict[str, list[UnifiedSource]] = {}
    for r in scored:
        by_key.setdefault(r.tool_key, []).append(r)

    has_twitter = "twitter" in enabled_keys
    web_keys = [k for k in enabled_keys if k != "twitter"]

    selected: list[UnifiedSource] = []
    if has_twitter and web_keys:
        tw = by_key.get("twitter", [])[:_TOP_TWITTER_MIXED]
        web_only = [r for r in scored if r.tool_key != "twitter"][:_TOP_WEB_MIXED]
        selected = tw + web_only
    elif has_twitter:
        selected.extend(by_key.get("twitter", [])[:_TOP_TWITTER_SOLO])
    else:
        web_only = [r for r in scored if r.tool_key != "twitter"]
        selected.extend(web_only[:_TOP_WEB_SOLO])

    seen: set[str] = set()
    deduped: list[UnifiedSource] = []
    for s in selected:
        lk = s.link.lower().rstrip("/")
        if lk in seen:
            continue
        seen.add(lk)
        deduped.append(s)
    return deduped[:_MAX_FINAL_SOURCES]


async def _build_summary_from_selected(
    query: str,
    date_filter_type: str | None,
    summary_sources: list[UnifiedSource],
) -> tuple[str, dict[str, ContextualRelevance]]:
    """
    Miner scores use all summary_sources (up to 10). LLM sees only the first
    _SUMMARY_LLM_SOURCE_COUNT sources as compact title/snippet blocks; cites [n] then we expand to URLs.
    """
    if not summary_sources:
        return (
            "**Findings**\nNo relevant sources were found.\n\n"
            "**Conclusion**\nThe query needs broader terms or updated data.",
            {},
        )
    llm_sources = summary_sources[:_SUMMARY_LLM_SOURCE_COUNT]
    client = _openai_client()
    t_parallel = time.perf_counter()
    try:
        summary_text, miner_scores = await asyncio.gather(
            _run_compact_summary_llm(client, query, llm_sources),
            _run_link_labels_llm(client, query, summary_sources),
        )
    except Exception:
        summary_text = _ensure_summary_structure(
            _build_fast_cited_summary(query, llm_sources)
        )
        miner_scores = _miner_link_scores_raw_keywords(query, summary_sources)
    _log(f"timing.parallel_summary_and_labels={time.perf_counter() - t_parallel:.3f}s")
    return summary_text, miner_scores


async def run_ai_solution(
    synapse: ScraperStreamingSynapse,
    task_meta: dict[str, Any] | None = None,
) -> ScraperStreamingSynapse:
    start = time.perf_counter()
    task_meta = task_meta or {}

    analyze_start = time.perf_counter()
    prompt = _normalize_whitespace(synapse.prompt)
    planned_query = await _plan_query_if_needed(prompt, task_meta)
    analyze_elapsed = time.perf_counter() - analyze_start
    _log(f"prompt={prompt}")
    _log(f"planned_query={planned_query}")
    _log(f"timing.analyze={analyze_elapsed:.3f}s")
    max_items = int(getattr(synapse, "count", None) or _DEFAULT_MAX_ITEMS)
    per_tool_items = _OTHER_FETCH_ITEMS

    tools = list(synapse.tools or []) or list(_TOOL_TO_KEY.keys())
    enabled_keys = []
    for t in tools:
        key = _TOOL_TO_KEY.get(t)
        if key and key not in enabled_keys:
            enabled_keys.append(key)
    _log(f"enabled_tools={enabled_keys}")

    tool_queries = {
        key: _tool_query_from_analysis(prompt, task_meta, key, planned_query)
        for key in enabled_keys
    }
    for key, q in tool_queries.items():
        _log(f"tool_query[{key}]={q}")

    async def _run_tool_once(tool_key: str, factory):
        t0 = time.perf_counter()
        try:
            result = await factory()
            elapsed = time.perf_counter() - t0
            count = len(result or [])
            if count > 0:
                _log(
                    f"timing.tool.{tool_key}={elapsed:.3f}s "
                    f"status=ok attempt=1 count={count}"
                )
                return list(result or [])
            _log(
                f"timing.tool.{tool_key}={elapsed:.3f}s "
                f"status=empty attempt=1"
            )
            return []
        except Exception as e:
            elapsed = time.perf_counter() - t0
            _log(
                f"timing.tool.{tool_key}={elapsed:.3f}s "
                f"status=error attempt=1 error={type(e).__name__}: {e}"
            )
            return []

    tasks: dict[str, asyncio.Task] = {}
    if "twitter" in enabled_keys:
        twitter_query = tool_queries.get("twitter", planned_query)
        tasks["twitter"] = asyncio.create_task(
            _run_tool_once(
                "twitter",
                lambda: _run_twitter(
                    twitter_query, per_tool_items, synapse.date_filter_type
                ),
            )
        )
    if "web" in enabled_keys:
        web_query = tool_queries.get("web", planned_query)
        tasks["web"] = asyncio.create_task(
            _run_tool_once(
                "web", lambda: _run_web(web_query, per_tool_items)
            )
        )
    if "reddit" in enabled_keys:
        reddit_query = tool_queries.get("reddit", planned_query)
        tasks["reddit"] = asyncio.create_task(
            _run_tool_once(
                "reddit",
                lambda: _run_thread(_run_reddit_sync, reddit_query, per_tool_items),
            )
        )
    if "youtube" in enabled_keys:
        youtube_query = tool_queries.get("youtube", planned_query)
        tasks["youtube"] = asyncio.create_task(
            _run_tool_once(
                "youtube",
                lambda: _run_thread(_run_youtube_sync, youtube_query, per_tool_items),
            )
        )
    if "arxiv" in enabled_keys:
        arxiv_query = tool_queries.get("arxiv", planned_query)
        tasks["arxiv"] = asyncio.create_task(
            _run_tool_once(
                "arxiv",
                lambda: _run_thread(_run_arxiv_sync, arxiv_query, per_tool_items),
            )
        )
    if "hackernews" in enabled_keys:
        hn_query = tool_queries.get("hackernews", planned_query)
        tasks["hackernews"] = asyncio.create_task(
            _run_tool_once(
                "hackernews",
                lambda: _run_thread(_run_hn_sync, hn_query, per_tool_items),
            )
        )
    if "wikipedia" in enabled_keys:
        wiki_query = tool_queries.get("wikipedia", planned_query)
        tasks["wikipedia"] = asyncio.create_task(
            _run_tool_once(
                "wikipedia",
                lambda: _run_thread(_run_wikipedia_sync, wiki_query, per_tool_items),
            )
        )

    by_tool: dict[str, list[dict[str, Any]]] = {}
    tools_stage_start = time.perf_counter()
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    for key, result in zip(tasks.keys(), results):
        if isinstance(result, Exception):
            _log(f"tool_result[{key}] error={type(result).__name__}: {result}")
            by_tool[key] = []
        else:
            by_tool[key] = list(result or [])
        sample = by_tool[key][0] if by_tool[key] else {}
        _log(f"tool_result[{key}]: count={len(by_tool[key])} sample={json.dumps(sample, ensure_ascii=False)[:300]}")
    tools_stage_elapsed = time.perf_counter() - tools_stage_start
    _log(f"timing.tools_all={tools_stage_elapsed:.3f}s")

    normalize_start = time.perf_counter()
    rows = _collect_source_rows(by_tool)
    normalize_elapsed = time.perf_counter() - normalize_start
    _log(f"unified_sources={len(rows)}")
    _log(f"timing.normalize_sources={normalize_elapsed:.3f}s")

    summary_stage_start = time.perf_counter()
    selected_rows = _pick_selected_sources(
        planned_query,
        by_tool=by_tool,
        normalized_rows=rows,
        enabled_keys=enabled_keys,
    )
    _log_selected_sources_detail(selected_rows)
    by_tool_final = _trim_by_tool_to_selected(by_tool, selected_rows)
    summary, miner_link_scores = await _build_summary_from_selected(
        planned_query,
        synapse.date_filter_type,
        summary_sources=selected_rows,
    )
    summary_stage_elapsed = time.perf_counter() - summary_stage_start
    _log(
        f"top_ranked_sources={len(selected_rows)} "
        f"(cap≤{_MAX_FINAL_SOURCES}: solo tw/web {_TOP_TWITTER_SOLO}/{_TOP_WEB_SOLO} "
        f"mixed {_TOP_TWITTER_MIXED}+{_TOP_WEB_MIXED})"
    )
    _log(f"miner_link_scores_keys={len(miner_link_scores)}")
    _log(f"summary_chars={len(summary)}")
    _log(f"timing.select_and_summary={summary_stage_elapsed:.3f}s")

    synapse.miner_tweets = by_tool_final.get("twitter", [])
    synapse.search_results = by_tool_final.get("web", [])
    synapse.reddit_search_results = by_tool_final.get("reddit", [])
    synapse.youtube_search_results = by_tool_final.get("youtube", [])
    synapse.arxiv_search_results = by_tool_final.get("arxiv", [])
    synapse.hacker_news_search_results = by_tool_final.get("hackernews", [])
    synapse.wikipedia_search_results = by_tool_final.get("wikipedia", [])
    synapse.miner_link_scores = miner_link_scores
    synapse.result_type = ResultType.LINKS_WITH_FINAL_SUMMARY
    synapse.completion = "completed"
    synapse.text_chunks = synapse.text_chunks or {}
    synapse.text_chunks[ScraperTextRole.FINAL_SUMMARY.value] = _stream_safe_chunks(summary)
    work_elapsed = time.perf_counter() - start
    if work_elapsed < _MIN_SOLUTION_WALL_SECONDS:
        pad = _MIN_SOLUTION_WALL_SECONDS - work_elapsed
        _log(
            f"timing.pad_to_minimum={pad:.3f}s "
            f"(work={work_elapsed:.3f}s target≥{_MIN_SOLUTION_WALL_SECONDS}s)"
        )
        await asyncio.sleep(pad)
    total_elapsed = time.perf_counter() - start
    _log(f"timing.total_solution={total_elapsed:.3f}s")
    synapse.dendrite = {"status_code": 200, "process_time": total_elapsed}
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
