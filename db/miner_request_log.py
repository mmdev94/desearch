"""Persist miner request/response summaries to Postgres (same ``DATABASE_URL`` as ``setup_db``)."""

from __future__ import annotations

import json
import traceback
from typing import Any

from psycopg.types.json import Json

from db.pg import connect, load_env

_TABLE_BY_KIND = {
    "x_search": "public.miner_request_log_x_search",
    "web_search": "public.miner_request_log_web_search",
    "ai_search": "public.miner_request_log_ai_search",
}


def _conn():
    load_env()
    return connect()


def _json_payload(data: Any, max_len: int = 120_000) -> Json:
    try:
        s = json.dumps(data, default=str, ensure_ascii=False)
    except Exception:
        return Json({"error": "serialization_failed"})
    if len(s) > max_len:
        s = s[: max_len - 40] + '","truncated":true}'
    try:
        return Json(json.loads(s))
    except json.JSONDecodeError:
        return Json({"error": "serialization_failed", "raw_prefix": s[:2000]})


def log_miner_request(
    kind: str,
    *,
    request_payload: dict[str, Any],
    duration_ms: float,
    status: str = "ok",
    error: str | None = None,
    dendrite_hotkey: str | None = None,
    validator_uid: int | None = None,
    result_status: str | None = None,
) -> None:
    table = _TABLE_BY_KIND.get(kind)
    if not table:
        return
    rs = result_status or ("success" if status == "ok" else "fail")
    try:
        conn = _conn()
    except Exception:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {table}
                  (dendrite_hotkey, validator_uid, result_status, status, error,
                   duration_ms, request_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    dendrite_hotkey,
                    validator_uid,
                    rs,
                    status,
                    error,
                    float(duration_ms),
                    _json_payload(request_payload),
                ),
            )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()


def safe_log_miner_request(
    kind: str,
    *,
    request_payload: dict[str, Any],
    duration_ms: float,
    exc: BaseException | None = None,
    dendrite_hotkey: str | None = None,
    validator_uid: int | None = None,
) -> None:
    if exc is None:
        log_miner_request(
            kind,
            request_payload=request_payload,
            duration_ms=duration_ms,
            status="ok",
            error=None,
            dendrite_hotkey=dendrite_hotkey,
            validator_uid=validator_uid,
            result_status="success",
        )
        return
    log_miner_request(
        kind,
        request_payload=request_payload,
        duration_ms=duration_ms,
        status="error",
        error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"[:8000],
        dendrite_hotkey=dendrite_hotkey,
        validator_uid=validator_uid,
        result_status="fail",
    )
