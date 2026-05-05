#!/usr/bin/env python3
"""
Fulfillment: Apify browser signup + default personal API token.

Reuses ``apify/lib/auto-signup.py`` (Smailpro temp Outlook, Apify sign-up, email verify).
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
  python3 fulfillment/scripts/apify_fulfillment_create_account.py
  python3 fulfillment/scripts/apify_fulfillment_create_account.py --count 5

``--count 0`` (default) runs until you interrupt (Ctrl+C). A positive ``--count N`` stops
after N attempts (each attempt is one new browser + signup flow).
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
_APIFY_LIB = _REPO_ROOT / "apify" / "lib"
for _p in (_APIFY_LIB, _REPO_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _load_auto_signup():
    p = _APIFY_LIB / "auto-signup.py"
    spec = importlib.util.spec_from_file_location("apify_auto_signup_fulfillment", p)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {p}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--count",
        type=int,
        default=0,
        help="Stop after this many attempts (each = new Chrome). 0 = run forever (default).",
    )
    args = ap.parse_args()

    from db.pg import load_env  # noqa: PLC0415

    load_env()

    from apify_scrape_flow import build_uc_driver  # noqa: E402
    from paths_layout import DEFAULT_PROXIES_FILE  # noqa: E402
    from proxy_pool import pick_proxy  # noqa: E402

    signup = _load_auto_signup()
    create_acc = signup.create_single_apify_account

    proxies_path = DEFAULT_PROXIES_FILE
    if not isinstance(proxies_path, Path):
        proxies_path = Path(proxies_path)

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
            if px:
                print(f"[fulfillment attempt {label}] Using proxy: {px.url}")

            driver = build_uc_driver(
                proxy_server=px.url if px else None,
                user_data_dir=None,
                headless=False,
            )
            try:
                print(f"\n[fulfillment attempt {label}] Apify signup + integrations API token…")
                created = create_acc(
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
