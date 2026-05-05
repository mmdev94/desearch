"""Twikit-backed Twitter solution (credential login, no Apify)."""

from solutions.twitter1.id import search_by_id
from solutions.twitter1.query import search
from solutions.twitter1.url import search_by_urls

__all__ = ["search", "search_by_id", "search_by_urls"]
