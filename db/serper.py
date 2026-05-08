"""Serper API keys and credits stored in Postgres (``public.serper_api_key``)."""

from __future__ import annotations

from db.pg import connect, load_env


def fetch_all_keys() -> list[tuple[str, int]]:
    """Return ``(api_key, credits)`` rows ordered by credits descending."""
    load_env()
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT api_key, credits
                FROM public.serper_api_key
                ORDER BY credits DESC
                """
            )
            rows = cur.fetchall()
        return [(str(r[0]), max(0, int(r[1]))) for r in rows]
    finally:
        conn.close()


def debit_credits(api_key: str, debit: int) -> None:
    """Subtract ``debit`` from the row for ``api_key`` (floored at 0)."""
    if debit <= 0:
        return
    load_env()
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.serper_api_key
                SET credits = GREATEST(0, credits - %s)
                WHERE api_key = %s
                """,
                (int(debit), api_key),
            )
        conn.commit()
    finally:
        conn.close()

