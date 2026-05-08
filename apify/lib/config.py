"""Apify automation settings (edit locally; avoid committing secrets)."""

from datetime import date
from pathlib import Path

# When True, flows load `leadpoet-miner/apify/proxies/proxies.txt` (or CLI --proxies-file) and pass
# a proxy to Chrome. final_auto_flow picks a stable line per worker (round-robin by worker_id).
PROXY = False

# When True, each `build_uc_driver()` run picks a realistic UA + viewport + TZ + locale (see browser_fingerprint.py).
BROWSER_FINGERPRINT = True

# When True, use human-like pauses/click/type helpers in signup/signin/scrape flows.
# When False, skip synthetic pauses and use direct click/type for faster execution.
HUMAN_BEHAVIOR = False

# When True, add --incognito (only if no custom --user-data-dir is passed to build_uc_driver).
INCOGNITO = False

# final_auto_flow: one persistent Chrome profile directory per worker
# (<CHROME_PROFILES_ROOT>/worker_00, worker_01, …). Avoids sharing one user-data dir across drivers.
CHROME_USE_WORKER_PROFILES = False
# This file lives in apify/lib/; repo root is three levels up from here.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
# chrome_profiles at repo root (same layout as when config lived in apify/)
CHROME_PROFILES_ROOT = _REPO_ROOT / "chrome_profiles"
# Subdir inside user-data-dir (Chrome default is "Default")
CHROME_PROFILE_DIRECTORY = "Default"

# After driver starts, CDP-clear cookies/cache and Apify origins' storage before signup/signin.
CLEAR_CHROME_STORAGE_BEFORE_ACCOUNT_SESSION = True

# Smailpro / Sonjj temp Outlook: max attempts to obtain a non-blacklisted domain before giving up.
EMAIL_DOMAIN_BLACKLIST_MAX_INBOX_ATTEMPTS = 40

# auto-signup: if Apify shows "This email is already taken", retry from new Outlook inbox (same browser).
APIFY_SIGNUP_EMAIL_TAKEN_MAX_ROUNDS = 30

# --- Apify actor output / dataset scrape (apify_scrape_flow.run_one_apify_search) ---
# After the browser reaches the run output URL, wait at least this long (wall clock) before polling
# for the dataset table. Reduces false "ready" when the run is still starting.
MIN_WAIT_SEC_BEFORE_TABLE_DETECT = 10.0
# After min wait, poll `[data-test="actor-run-summary-status"] [status]` until it is not RUNNING
# (e.g. SUCCEEDED). Prevents scraping while the run is still writing rows. Set to 0 to disable.
MAX_WAIT_SEC_FOR_ACTOR_RUN_STATUS = 600.0
# After the table shell exists, keep scrolling/polling until we see at least one exportable row
# (non-loading, with email) or this many seconds pass. Set to 0 to disable.
MAX_WAIT_SEC_FOR_EXPORT_ROWS_AFTER_TABLE = 45.0
# Wall time budget for virtualized page-1 scrape + optional page-2 after the table is ready.
SCRAPE_TWO_PAGES_BUDGET_SEC = 120.0

# Actor input page: if JSON tab or Monaco editor is missing, refresh and retry until this budget
# elapses (avoids stopping a lead search on slow/hydrated UI).
ACTOR_INPUT_JSON_UI_RESOLVE_BUDGET_SEC = 300.0
# Per-try WebDriver wait cap for JSON tab or Monaco textarea before a refresh (seconds).
ACTOR_INPUT_JSON_UI_ATTEMPT_TIMEOUT_SEC = 25.0

# Terminal: Space bar pauses/resumes automation (see apify/lib/flow_pause.py). Requires a TTY.
FLOW_PAUSE_SPACE_TOGGLE = True

# final_auto_flow: before this calendar date, use signup-first (Mon–Wed style) every day and skip
# Monday weekly pool resets. On/after this date, normal Mon–Wed vs Thu–Sun + Monday cycles apply.
# Set to None to always use the real calendar (no pre-production window).
FINAL_FLOW_CYCLE_LOGIC_START_DATE: date | None = date(2026, 4, 6)
