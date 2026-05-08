#!/usr/bin/env python3
"""
Reset DB schema for automation account storage.

Behavior:
- Drops all tables in ``public`` except ``apify_account``, ``twex_account``, ``serper_api_key``,
  and the three miner request log tables (see below).
- Creates/ensures account tables, Serper keys, and miner request log tables (idempotent).

``apify_account`` and ``twex_account`` both hold automation fields:
email, password, API key/token and credit amount, with unique lower(email).

On upgrade, rows with ``credit_amount`` NULL are backfilled:
- ``apify_account``: **2.0** when ``password`` is not null, else **0.0**.
- ``twex_account``: **20000.0** when ``email``, ``password`` and ``api_key`` are all present,
  else **0.0**.
Existing non-NULL ``credit_amount`` values are left unchanged.

Usage:
  python db/setup_db.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from db.pg import connect, load_env  # noqa: E402


DROP_OTHER_PUBLIC_TABLES_SQL = """
DO $$
DECLARE
  r RECORD;
BEGIN
  FOR r IN
    SELECT tablename
    FROM pg_tables
    WHERE schemaname = 'public'
      AND tablename NOT IN (
        'apify_account',
        'twex_account',
        'serper_api_key',
        'miner_request_log_x_search',
        'miner_request_log_web_search',
        'miner_request_log_ai_search'
      )
  LOOP
    EXECUTE format('DROP TABLE IF EXISTS public.%I CASCADE', r.tablename);
  END LOOP;
END $$;
"""

CREATE_APIFY_ACCOUNT_SQL = """
CREATE TABLE IF NOT EXISTS public.apify_account (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    email TEXT NOT NULL,
    password TEXT,
    full_name TEXT,
    mailbox_password TEXT,
    mailbox_token TEXT,
    api_token TEXT,
    credit_amount DOUBLE PRECISION NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_apify_account_email_lower
  ON public.apify_account (lower(trim(email)));
"""

CREATE_TWEX_ACCOUNT_SQL = """
CREATE TABLE IF NOT EXISTS public.twex_account (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    email TEXT NOT NULL,
    password TEXT,
    api_key TEXT,
    credit_amount DOUBLE PRECISION NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_twex_account_email_lower
  ON public.twex_account (lower(trim(email)));
"""

CREATE_SERPER_API_KEY_SQL = """
CREATE TABLE IF NOT EXISTS public.serper_api_key (
    api_key TEXT PRIMARY KEY,
    credits INTEGER NOT NULL DEFAULT 2460 CHECK (credits >= 0)
);
CREATE INDEX IF NOT EXISTS idx_serper_api_key_credits ON public.serper_api_key (credits DESC);
"""

# Three logically separate request logs (one table per search type / synapse family).
_CREATE_MINER_LOG_TEMPLATE = """
CREATE TABLE IF NOT EXISTS public.{name} (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    dendrite_hotkey TEXT,
    validator_uid INTEGER,
    result_status TEXT NOT NULL DEFAULT 'unknown',
    status TEXT NOT NULL DEFAULT 'ok',
    error TEXT,
    duration_ms DOUBLE PRECISION,
    request_json JSONB NOT NULL DEFAULT '{{}}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_{name}_created_at ON public.{name} (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_{name}_hotkey ON public.{name} (dendrite_hotkey);
CREATE INDEX IF NOT EXISTS idx_{name}_uid ON public.{name} (validator_uid);
CREATE INDEX IF NOT EXISTS idx_{name}_result ON public.{name} (result_status);
"""

_MINER_LOG_UPGRADE_SQL = (
    "ALTER TABLE public.miner_request_log_x_search ADD COLUMN IF NOT EXISTS validator_uid INTEGER",
    "ALTER TABLE public.miner_request_log_x_search ADD COLUMN IF NOT EXISTS result_status TEXT NOT NULL DEFAULT 'unknown'",
    "ALTER TABLE public.miner_request_log_web_search ADD COLUMN IF NOT EXISTS validator_uid INTEGER",
    "ALTER TABLE public.miner_request_log_web_search ADD COLUMN IF NOT EXISTS result_status TEXT NOT NULL DEFAULT 'unknown'",
    "ALTER TABLE public.miner_request_log_ai_search ADD COLUMN IF NOT EXISTS validator_uid INTEGER",
    "ALTER TABLE public.miner_request_log_ai_search ADD COLUMN IF NOT EXISTS result_status TEXT NOT NULL DEFAULT 'unknown'",
)

CREATE_MINER_REQUEST_LOGS_SQL = "\n".join(
    [
        _CREATE_MINER_LOG_TEMPLATE.format(name="miner_request_log_x_search"),
        _CREATE_MINER_LOG_TEMPLATE.format(name="miner_request_log_web_search"),
        _CREATE_MINER_LOG_TEMPLATE.format(name="miner_request_log_ai_search"),
    ]
)

_APIFY_ACCOUNT_UPGRADE_SQL = (
    "ALTER TABLE public.apify_account ADD COLUMN IF NOT EXISTS api_token TEXT",
    "ALTER TABLE public.apify_account ADD COLUMN IF NOT EXISTS credit_amount DOUBLE PRECISION",
    r"""
UPDATE public.apify_account
SET credit_amount = CASE
    WHEN password IS NOT NULL THEN 2.0
    ELSE 0.0
END
WHERE credit_amount IS NULL
""".strip(),
    "ALTER TABLE public.apify_account ALTER COLUMN credit_amount SET DEFAULT 0",
    "ALTER TABLE public.apify_account ALTER COLUMN credit_amount SET NOT NULL",
    "ALTER TABLE public.apify_account DROP COLUMN IF EXISTS api_status",
    "ALTER TABLE public.apify_account DROP COLUMN IF EXISTS api_run_count",
)

_TWEX_ACCOUNT_UPGRADE_SQL = (
    "ALTER TABLE public.twex_account ADD COLUMN IF NOT EXISTS api_key TEXT",
    "ALTER TABLE public.twex_account ADD COLUMN IF NOT EXISTS credit_amount DOUBLE PRECISION",
    r"""
UPDATE public.twex_account
SET credit_amount = CASE
    WHEN email IS NOT NULL
         AND trim(email) <> ''
         AND password IS NOT NULL
         AND trim(password) <> ''
         AND api_key IS NOT NULL
         AND trim(api_key) <> '' THEN 20000.0
    ELSE 0.0
END
WHERE credit_amount IS NULL
""".strip(),
    "ALTER TABLE public.twex_account ALTER COLUMN credit_amount SET DEFAULT 0",
    "ALTER TABLE public.twex_account ALTER COLUMN credit_amount SET NOT NULL",
)


def main() -> int:
    load_env()
    try:
        conn = connect()
    except Exception as e:
        print(f"Connection failed: {e}", file=sys.stderr)
        print("Set DATABASE_URL in the environment or repo-root .env.", file=sys.stderr)
        return 1

    try:
        with conn.cursor() as cur:
            cur.execute(DROP_OTHER_PUBLIC_TABLES_SQL)
            cur.execute(CREATE_APIFY_ACCOUNT_SQL)
            cur.execute(CREATE_TWEX_ACCOUNT_SQL)
            cur.execute(CREATE_SERPER_API_KEY_SQL)
            cur.execute(CREATE_MINER_REQUEST_LOGS_SQL)
            for stmt in _MINER_LOG_UPGRADE_SQL:
                cur.execute(stmt)
            for stmt in _APIFY_ACCOUNT_UPGRADE_SQL:
                cur.execute(stmt)
            for stmt in _TWEX_ACCOUNT_UPGRADE_SQL:
                cur.execute(stmt)
            cur.execute(
                "ALTER TABLE public.apify_account ALTER COLUMN password DROP NOT NULL"
            )
            cur.execute(
                "ALTER TABLE public.twex_account ALTER COLUMN password DROP NOT NULL"
            )
        conn.commit()
        print(
            "DB reset complete: apify_account + twex_account + serper_api_key + "
            "miner request logs are ready (other public tables dropped)."
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
