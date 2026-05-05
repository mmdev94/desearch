"""AI search solution helpers (Hacker News, ArXiv, etc.)."""

from solutions.ai.arxiv_search import (
    ArxivQuery,
    arxiv_search,
    fill_arxiv_results,
    run_arxiv_search_sync,
)
from solutions.ai.hacker_news import (
    HackerNewsQuery,
    fill_hacker_news_results,
    hn_algolia_search,
    run_hn_algolia_search_sync,
)
from solutions.ai.wikipedia_api_search import (
    WikipediaQuery,
    fill_wikipedia_results,
    run_wikipedia_search_sync,
    wikipedia_search,
)

__all__ = [
    "ArxivQuery",
    "arxiv_search",
    "fill_arxiv_results",
    "run_arxiv_search_sync",
    "HackerNewsQuery",
    "fill_hacker_news_results",
    "hn_algolia_search",
    "run_hn_algolia_search_sync",
    "WikipediaQuery",
    "fill_wikipedia_results",
    "run_wikipedia_search_sync",
    "wikipedia_search",
]

