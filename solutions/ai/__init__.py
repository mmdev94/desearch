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
from solutions.ai.reddit_search import (
    RedditQuery,
    arctic_reddit_posts_search,
    fill_reddit_results,
    parse_subreddit_from_prompt,
    reddit_discussion_url,
    run_arctic_reddit_search_sync,
)
from solutions.ai.youtube_search_pkg import (
    YoutubeQuery,
    fill_youtube_results,
    run_youtube_search_sync,
    youtube_search,
)
from solutions.ai.ai import (
    build_ai_synapse_from_task,
    run_ai_solution,
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
    "YoutubeQuery",
    "fill_youtube_results",
    "run_youtube_search_sync",
    "youtube_search",
    "RedditQuery",
    "arctic_reddit_posts_search",
    "fill_reddit_results",
    "parse_subreddit_from_prompt",
    "reddit_discussion_url",
    "run_arctic_reddit_search_sync",
    "run_ai_solution",
    "build_ai_synapse_from_task",
]

