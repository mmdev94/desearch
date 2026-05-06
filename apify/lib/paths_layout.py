"""
Default directory layout under ``apify/``:

- ``exports/`` — scraped CSV (e.g. ``apify_leads_export.csv``)
- ``accounts/`` — ``created_accounts.txt``, ``email_domain_blacklist.txt`` (domains to skip at signup)
- ``state/`` — JSON runtime state (accounts pool, cycle, search status, flow progress)
- ``proxies/`` — ``proxies.txt``
- Sonjj / Smailpro (web UI): ``SONJJ_ACCOUNT_EMAIL`` and ``SONJJ_ACCOUNT_PASSWORD`` in ``apify/.env``
- ``filters/`` — filter index shards (``.idx``) and ``filter_space_meta.json`` (formerly ``leads/``)

- ``datasets/`` — static filter JSON used by ``us_uk_filter_codec`` (``sizes.json``, ``industries.json``,
  ``location_groups.json``, ``raw_job_titles.json``, …) and produced by ``generation/`` scripts.

Optional subdirs (``validation/``, ``merge/``, …) may exist for local lead tooling; DB setup for this repo
is ``db/setup_db.py`` at the repository root (``apify_account`` for Apify credentials).
"""
from __future__ import annotations

from pathlib import Path

_LIB = Path(__file__).resolve().parent
APIFY_ROOT = _LIB.parent

EXPORTS_DIR = APIFY_ROOT / "exports"
ACCOUNTS_DIR = APIFY_ROOT / "accounts"
STATE_DIR = APIFY_ROOT / "state"
PROXIES_DIR = APIFY_ROOT / "proxies"
FILTERS_DIR = APIFY_ROOT / "filters"
DATASETS_DIR = APIFY_ROOT / "datasets"
VALIDATION_DIR = APIFY_ROOT / "validation"
MERGE_DIR = APIFY_ROOT / "merge"


def filter_datasets_dir(apify_root: Path) -> Path:
    """Directory for filter JSON inputs (under ``apify_root``, default ``apify/datasets``)."""
    return apify_root / "datasets"

DEFAULT_LEADS_CSV = EXPORTS_DIR / "apify_leads_export.csv"
DEFAULT_MERGE_DIR = MERGE_DIR
DEFAULT_MERGE_RESULT_CSV = MERGE_DIR / "result.csv"
DEFAULT_MERGE_VALIDATED_CSV = MERGE_DIR / "result_validated.csv"
DEFAULT_CREATED_ACCOUNTS = ACCOUNTS_DIR / "created_accounts.txt"
DEFAULT_EMAIL_DOMAIN_BLACKLIST = ACCOUNTS_DIR / "email_domain_blacklist.txt"
DEFAULT_ACCOUNTS_STATE = STATE_DIR / "apify_accounts_state.json"
DEFAULT_CYCLE_STATE = STATE_DIR / "final_cycle_state.json"
DEFAULT_SEARCH_STATUS = STATE_DIR / "final_search_status.json"
DEFAULT_FLOW_PROGRESS = STATE_DIR / "apify_flow_progress.json"
DEFAULT_PROXIES_FILE = PROXIES_DIR / "proxies.txt"
