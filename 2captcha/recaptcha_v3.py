from __future__ import annotations

import os
import time
from dataclasses import dataclass

import requests
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import ConnectTimeout, ReadTimeout


@dataclass(frozen=True)
class RecaptchaV3Solution:
    token: str
    task_id: str


class TwoCaptchaError(RuntimeError):
    pass


def _api_key_from_env() -> str:
    key = (os.environ.get("2CAPTCHA_API_KEY") or "").strip()
    if not key:
        raise TwoCaptchaError("Missing env var 2CAPTCHA_API_KEY")
    return key


def solve_recaptcha_v3(
    *,
    sitekey: str,
    pageurl: str,
    action: str | None = None,
    min_score: float = 0.3,
    is_enterprise: bool = False,
    api_domain: str = "google.com",
    timeout_seconds: int = 240,
    poll_seconds: float = 5.0,
) -> RecaptchaV3Solution:
    """
    Solve reCAPTCHA v3 via 2Captcha API v2.

    Docs: https://2captcha.com/api-docs/recaptcha-v3
    """
    key = _api_key_from_env()
    if not sitekey or not pageurl:
        raise TwoCaptchaError("sitekey and pageurl are required")

    task: dict[str, object] = {
        "type": "RecaptchaV3TaskProxyless",
        "websiteURL": pageurl,
        "websiteKey": sitekey,
        "minScore": float(min_score),
        "isEnterprise": bool(is_enterprise),
        "apiDomain": str(api_domain),
    }
    if action:
        task["pageAction"] = str(action)

    create_payload: dict[str, object] = {"clientKey": key, "task": task}

    http_timeout = float(os.environ.get("TWO_CAPTCHA_HTTP_TIMEOUT_SEC", "60") or "60")
    create_deadline = time.time() + min(60.0, max(10.0, float(timeout_seconds)))
    while True:
        try:
            resp = requests.post(
                "https://api.2captcha.com/createTask",
                json=create_payload,
                timeout=http_timeout,
            )
            data = resp.json()
            break
        except (ReadTimeout, ConnectTimeout, RequestsConnectionError) as exc:
            if time.time() >= create_deadline:
                raise TwoCaptchaError(
                    f"2Captcha createTask request failed (network timeout): {type(exc).__name__}: {exc}"
                ) from exc
            time.sleep(1.5)
        except Exception as exc:
            raise TwoCaptchaError(
                f"2Captcha createTask request failed: {type(exc).__name__}: {exc}"
            ) from exc

    if not isinstance(data, dict) or int(data.get("errorId", 1)) != 0:
        raise TwoCaptchaError(f"2Captcha createTask error: {data!r}")
    task_id = data.get("taskId")
    if not task_id:
        raise TwoCaptchaError(f"2Captcha createTask missing taskId: {data!r}")

    end = time.time() + max(20, int(timeout_seconds))
    time.sleep(min(10.0, max(3.0, float(poll_seconds))))

    backoff = max(1.0, float(poll_seconds))
    while time.time() < end:
        try:
            r = requests.post(
                "https://api.2captcha.com/getTaskResult",
                json={"clientKey": key, "taskId": task_id},
                timeout=http_timeout,
            )
            out = r.json()
        except (ReadTimeout, ConnectTimeout, RequestsConnectionError) as exc:
            time.sleep(min(15.0, backoff))
            backoff = min(15.0, backoff * 1.4)
            continue
        except Exception as exc:
            raise TwoCaptchaError(
                f"2Captcha getTaskResult request failed: {type(exc).__name__}: {exc}"
            ) from exc

        if not isinstance(out, dict) or int(out.get("errorId", 1)) != 0:
            raise TwoCaptchaError(f"2Captcha getTaskResult error: {out!r}")

        status = str(out.get("status") or "").strip().lower()
        if status == "processing":
            time.sleep(min(10.0, max(2.0, float(poll_seconds))))
            continue

        if status == "ready":
            sol = out.get("solution") if isinstance(out.get("solution"), dict) else {}
            token = str((sol or {}).get("gRecaptchaResponse") or (sol or {}).get("token") or "").strip()
            if not token:
                raise TwoCaptchaError(f"2Captcha ready but missing token: {out!r}")
            return RecaptchaV3Solution(token=token, task_id=str(task_id))

        raise TwoCaptchaError(f"2Captcha unexpected status: {out!r}")

    raise TwoCaptchaError("2Captcha timed out waiting for solution")

