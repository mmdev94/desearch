"""Postgres helpers: repo-root ``.env`` and ``DATABASE_URL``."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg

_REPO_ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    """Load ``.env`` from repo root (does not override existing environment keys)."""
    path = _REPO_ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        os.environ[key] = value


def connect():
    """Return a new ``psycopg`` connection using ``DATABASE_URL``."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("Set DATABASE_URL (e.g. in repo-root .env).")
    return psycopg.connect(url, autocommit=False)


def load_apify_env() -> None:
    """Load repo-root ``.env`` for Apify flows (same as :func:`load_env`)."""
    load_env()


@contextmanager
def connect_ctx() -> Iterator[psycopg.Connection]:
    """Context manager: open Postgres connection, always close on exit."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def sync_apify_account_id_sequence(conn: psycopg.Connection) -> None:
    """Align ``apify_account_id_seq`` with current ``MAX(id)`` after manual inserts."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT setval(
                pg_get_serial_sequence('public.apify_account', 'id'),
                COALESCE((SELECT MAX(id) FROM public.apify_account), 0),
                true
            )
            """
        )
