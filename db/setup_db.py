#!/usr/bin/env python3
"""
Reset DB schema for Apify account storage.

Behavior:
- Drops all tables in ``public`` except ``apify_account``.
- Creates/ensures ``apify_account`` and related indexes (idempotent).

``apify_account`` holds automation fields (email, password, full_name, mailbox fields,
``api_token``, ``credit_amount``, unique lower(email)). ``credit_amount`` is a float
(e.g. 0.5, 5.0) for usage/credits tracking.

On upgrade, rows with ``credit_amount`` NULL get backfilled: **2.0** when ``password``
is not null (active credentials), **0.0** otherwise. Existing non-NULL ``credit_amount``
values are left unchanged.

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
      AND tablename NOT IN ('apify_account')
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
            for stmt in _APIFY_ACCOUNT_UPGRADE_SQL:
                cur.execute(stmt)
            cur.execute(
                "ALTER TABLE public.apify_account ALTER COLUMN password DROP NOT NULL"
            )
        conn.commit()
        print(
            "DB reset complete: apify_account is ready "
            "(other public tables dropped)."
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
