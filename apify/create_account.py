#!/usr/bin/env python3
"""
Fulfillment: Apify browser signup + default personal API token.

Reuses ``apify/lib/auto_signup.py`` (Smailpro temp Outlook, Apify sign-up, email verify).
Onboarding display name comes from the Outlook address (local-part heuristic) and
optional local LLM (``OPENAI_BASE_URL`` / ``LOCAL_LLM_MODEL``; skip with ``APIFY_SKIP_NAME_LLM=1``).

After verification, opens `https://console.apify.com/settings/integrations`, reads the default
**Personal API tokens** value, and saves ``email``, ``password``, ``full_name``, ``api_token``,
and ``credit_amount`` (per ``db/setup_db.py`` schema) in Postgres ``apify_account``.

**Each attempt** starts a **new** Chrome session and **always** quits the browser when the
attempt finishes (success or failure), then the next attempt starts fresh.

Prerequisites: ``db/setup_db.py`` applied so ``apify_account`` includes ``api_token``,
``credit_amount``, etc.

Usage:
  poetry run python apify/create_account.py
  poetry run python apify/create_account.py --count 5

``--count 0`` (default) runs until you interrupt (Ctrl+C). A positive ``--count N`` stops
after N attempts (each attempt is one new browser + signup flow).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--count",
        type=int,
        default=0,
        help="Stop after this many attempts (each = new Chrome). 0 = run forever (default).",
    )
    args = ap.parse_args()

    from apify.lib.apify_scrape_flow import build_uc_driver  # noqa: PLC0415
    from apify.lib.auto_signup import create_single_apify_account  # noqa: PLC0415
    from db.pg import load_env  # noqa: PLC0415

    load_env()

    pick_proxy_fn = None
    proxies_path: Path | None = None
    try:
        from paths_layout import DEFAULT_PROXIES_FILE  # noqa: PLC0415
        from proxy_pool import pick_proxy as pick_proxy_fn  # noqa: PLC0415

        _pf = DEFAULT_PROXIES_FILE
        proxies_path = Path(_pf) if not isinstance(_pf, Path) else _pf
    except ImportError:
        pass

    max_attempts = int(args.count)
    infinite = max_attempts <= 0
    if infinite:
        print("[fulfillment] Running until Ctrl+C (each attempt uses a new Chrome session).")
    else:
        print(f"[fulfillment] Will run up to {max_attempts} attempt(s), new Chrome each time.")

    attempt = 1
    try:
        while True:
            if not infinite and attempt > max_attempts:
                break

            label = str(attempt) if infinite else f"{attempt}/{max_attempts}"
            px = None
            if pick_proxy_fn and proxies_path and proxies_path.is_file():
                try:
                    px = pick_proxy_fn(proxies_path)
                except Exception:
                    px = None
            if px:
                print(f"[fulfillment attempt {label}] Using proxy: {px.url}")

            driver = build_uc_driver(
                proxy_server=px.url if px else None,
                user_data_dir=None,
                headless=False,
            )
            try:
                print(f"\n[fulfillment attempt {label}] Apify signup + integrations API token…")
                created = create_single_apify_account(
                    driver,
                    append_to=None,
                    post_signup="integrations_token",
                )
                if not created:
                    print(f"[fulfillment attempt {label}] Signup or token capture failed.")
                else:
                    print(
                        f"[fulfillment attempt {label}] Saved apify_account for "
                        f"{created.get('email', '')!r} "
                        f"(api_token length={len(created.get('api_token', '') or '')})."
                    )
            except Exception as exc:
                print(
                    f"[fulfillment attempt {label}] Error during signup flow: "
                    f"{type(exc).__name__}: {exc}"
                )
            finally:
                try:
                    driver.quit()
                except Exception:
                    pass
                print(f"[fulfillment attempt {label}] Browser closed; starting next attempt…")

            attempt += 1
    except KeyboardInterrupt:
        print("\n[fulfillment] Interrupted.")


if __name__ == "__main__":
    main()
