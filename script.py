import json
import sys
from typing import Dict

import requests


BASE_URL = "https://twitterwebviewer.com"
SEARCH_URL = f"{BASE_URL}/api/search/tweets"


def _browser_headers() -> Dict[str, str]:
    return {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "referer": f"{BASE_URL}/",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "x-requested-with": "XMLHttpRequest",
    }


def fetch_tweets(query: str = "AI") -> str:
    session = requests.Session()
    session.headers.update(_browser_headers())

    # Warm up cookies/session similarly to normal browser page visit.
    session.get(f"{BASE_URL}/", timeout=30)

    response = session.get(
        SEARCH_URL,
        params={"q": query},
        timeout=30,
    )
    response.raise_for_status()

    # Print JSON compactly if valid JSON; otherwise print raw response.
    try:
        print(json.dumps(response.json(), ensure_ascii=False))
    except Exception:
        print(response.text)
    return response.text


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "AI"
    fetch_tweets(q)
