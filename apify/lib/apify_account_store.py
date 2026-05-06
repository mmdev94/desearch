#!/usr/bin/env python3
"""
Persist Apify automation accounts: usage (USD), daily search counts, 7-day reuse cooldown.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_STATE_NAME = "apify_accounts_state.json"


@dataclass
class AccountRecord:
    email: str
    password: str
    full_name: str = ""
    mailbox_password: str = ""
    mailbox_token: str = ""
    usage_usd: float = 0.0
    limit_usd: float = 5.0
    searches_today: int = 0
    last_search_day: str = ""  # YYYY-MM-DD (local)
    cooldown_until_ts: float = 0.0
    created_ts: float = field(default_factory=time.time)
    retired: bool = False
    retire_reason: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_json(d: dict[str, Any]) -> AccountRecord:
        return AccountRecord(
            email=str(d.get("email", "")),
            password=str(d.get("password", "")),
            full_name=str(d.get("full_name", "")),
            mailbox_password=str(d.get("mailbox_password", "")),
            mailbox_token=str(d.get("mailbox_token", "")),
            usage_usd=float(d.get("usage_usd", 0.0)),
            limit_usd=float(d.get("limit_usd", 5.0)),
            searches_today=int(d.get("searches_today", 0)),
            last_search_day=str(d.get("last_search_day", "")),
            cooldown_until_ts=float(d.get("cooldown_until_ts", 0.0)),
            created_ts=float(d.get("created_ts", time.time())),
            retired=bool(d.get("retired", False)),
            retire_reason=str(d.get("retire_reason", "")),
        )


def _today_str() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def load_state(path: Path) -> list[AccountRecord]:
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = raw.get("accounts") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    out: list[AccountRecord] = []
    for it in items:
        if isinstance(it, dict) and it.get("email"):
            out.append(AccountRecord.from_json(it))
    return out


def save_state(path: Path, accounts: list[AccountRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 1, "accounts": [a.to_json() for a in accounts]}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def reset_daily_counters_if_needed(rec: AccountRecord) -> None:
    day = _today_str()
    if rec.last_search_day != day:
        rec.searches_today = 0
        rec.last_search_day = day


def pick_eligible_account(
    accounts: list[AccountRecord],
    *,
    max_searches_per_day: int = 20,
    reuse_cooldown_seconds: float = 7 * 24 * 3600,
    max_usage_usd: float = 4.9,
    now: float | None = None,
) -> AccountRecord | None:
    """
    Pick a non-retired account under usage cap, under daily search limit,
    and not in cooldown window (unless last use was >= reuse_cooldown_seconds ago).
    """
    t = time.time() if now is None else now
    best: AccountRecord | None = None
    best_key: tuple[float, int] | None = None

    for a in accounts:
        if a.retired or not a.email or not a.password:
            continue
        if a.usage_usd >= max_usage_usd:
            continue
        reset_daily_counters_if_needed(a)
        if a.searches_today >= max_searches_per_day:
            continue
        if a.cooldown_until_ts > t:
            continue
        key = (a.usage_usd, a.searches_today)
        if best is None:
            best = a
            best_key = key
        elif best_key is not None and key < best_key:
            best = a
            best_key = key

    return best


def record_search_finished(
    rec: AccountRecord,
    *,
    usage_usd: float | None,
    searches_increment: int = 1,
    week_cooldown_when_daily_cap: bool = True,
    max_searches_per_day: int = 20,
) -> None:
    reset_daily_counters_if_needed(rec)
    rec.searches_today += searches_increment
    rec.last_search_day = _today_str()
    if usage_usd is not None:
        rec.usage_usd = float(usage_usd)
    if week_cooldown_when_daily_cap and rec.searches_today >= max_searches_per_day:
        rec.cooldown_until_ts = time.time() + 7 * 24 * 3600


def maybe_retire_high_usage(rec: AccountRecord, *, threshold: float = 4.9) -> None:
    if rec.usage_usd >= threshold:
        rec.retired = True
        rec.retire_reason = f"usage_usd>={threshold}"
