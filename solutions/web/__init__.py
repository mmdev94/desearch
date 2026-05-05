"""Drop-in miner helpers (e.g. Serper-backed web search)."""

from solutions.web.search import (
    SerperWebSearch,
    get_is_valid_web_search_result,
    run_web_search,
)

__all__: list[str] = ["SerperWebSearch", "run_web_search", "get_is_valid_web_search_result"]
