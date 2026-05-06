#!/usr/bin/env python3
"""
Apify console: sign in, JSON input, run actor, scrape dataset table (2 pages max).
After the output table appears, page 1 + page 2 scraping is bounded by config SCRAPE_TWO_PAGES_BUDGET_SEC.
"""

from __future__ import annotations

import csv
import http.client
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import undetected_chromedriver as uc
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException
from urllib3.exceptions import NewConnectionError, ProtocolError
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from .flow_pause import checkpoint, pause_aware_sleep  # noqa: E402
from .human_browser import (  # noqa: E402
    human_click_smooth as _hb_click_smooth,
    human_type_text_slow as _hb_type_text_slow,
    reset_synthetic_mouse,
)
from . import config as _apify_cfg  # noqa: E402

try:
    from urllib3.exceptions import MaxRetryError as _Urllib3MaxRetryError
except ImportError:  # pragma: no cover
    _Urllib3MaxRetryError = ()

_LIB_DIR = Path(__file__).resolve().parent


def is_webdriver_transport_error(exc: BaseException) -> bool:
    """
    True when Chrome/ChromeDriver closed the WebDriver HTTP pipe (urllib3/http.client),
    so the Selenium session is unusable and the driver should be recreated.
    """
    chain: list[BaseException] = []
    seen: set[int] = set()
    e: BaseException | None = exc
    while e is not None and id(e) not in seen and len(chain) < 16:
        seen.add(id(e))
        chain.append(e)
        e = e.__cause__ or e.__context__

    transport_types = (ProtocolError, NewConnectionError)
    if _Urllib3MaxRetryError:
        transport_types = (*transport_types, _Urllib3MaxRetryError)

    for err in chain:
        if isinstance(err, http.client.RemoteDisconnected):
            return True
        if type(err).__name__ == "RemoteDisconnected":
            return True
        if isinstance(err, transport_types):
            return True
        if isinstance(err, WebDriverException):
            msg = (str(err) or "").lower()
            for needle in (
                "connection refused",
                "remote end closed",
                "disconnected",
                "chrome not reachable",
                "invalid session id",
                "failed to establish a new connection",
                "connection aborted",
                "target window already closed",
                "no such window",
                "session deleted",
                "webdriver exception: connection",
            ):
                if needle in msg:
                    return True
    return False


APIFY_SIGN_IN_URL = "https://console.apify.com/sign-in"
# Override with APIFY_ACTOR_ID and/or APIFY_ACTOR_INPUT_URL for other actors (default: legacy lead actor).
_DEFAULT_LEGACY_ACTOR_ID = "IoSHqwTR9YGhzccez"
APIFY_ACTOR_ID = (os.environ.get("APIFY_ACTOR_ID") or "").strip() or _DEFAULT_LEGACY_ACTOR_ID
_env_actor_input_url = (os.environ.get("APIFY_ACTOR_INPUT_URL") or "").strip()
APIFY_ACTOR_INPUT_URL = (
    _env_actor_input_url
    if _env_actor_input_url
    else f"https://console.apify.com/actors/{APIFY_ACTOR_ID}/input"
)

# Default if not set in config.py (overridden by config.SCRAPE_TWO_PAGES_BUDGET_SEC at runtime).
SCRAPE_TWO_PAGES_BUDGET_SEC = 120.0
# Cap long polls so a stuck run does not block the flow for 10+ minutes.
WAIT_RUN_OUTPUT_URL_SEC = 240
WAIT_DATASET_TABLE_SEC = 120
# Max time to wait for actor run status to leave RUNNING (see wait_for_actor_run_not_running).
WAIT_ACTOR_RUN_STATUS_SEC = 600

_HUMAN_BEHAVIOR = bool(getattr(_apify_cfg, "HUMAN_BEHAVIOR", True))


def _rng_pause(min_s: float, max_s: float) -> None:
    """Human-paced delay; respects Space pause."""
    if _HUMAN_BEHAVIOR:
        pause_aware_sleep(random.uniform(min_s, max_s))
    else:
        checkpoint()


def _table_pause(min_s: float, max_s: float) -> None:
    """
    Table/render-sensitive waits are always kept human-like to avoid scraping
    partially hydrated rows (even when HUMAN_BEHAVIOR=False for click/type).
    """
    pause_aware_sleep(random.uniform(min_s, max_s))


def _sleep(seconds: float) -> None:
    if _HUMAN_BEHAVIOR:
        pause_aware_sleep(max(0.0, float(seconds)))
    else:
        checkpoint()


def human_click_smooth(driver, element) -> None:
    if _HUMAN_BEHAVIOR:
        _hb_click_smooth(driver, element)
        return
    try:
        element.click()
    except Exception:
        try:
            driver.execute_script("arguments[0].click();", element)
        except Exception:
            pass


def human_type_text_slow(element, text: str) -> None:
    if _HUMAN_BEHAVIOR:
        _hb_type_text_slow(element, text)
        return
    element.send_keys(text)

# Apify dataset table → CSV body column order (matches console export). Omit `keywords` (UI column
# after funding fields — we use filter JSON for sub_industry instead).
APIFY_LEADS_BODY_COLUMNS: tuple[str, ...] = (
    "first_name",
    "last_name",
    "email",
    "mobile_number",
    "personal_email",
    "company_name",
    "company_website",
    "linkedin",
    "full_name",
    "job_title",
    "industry",
    "headline",
    "seniority_level",
    "company_linkedin",
    "functional_level",
    "company_size",
    "city",
    "state",
    "country",
    "company_annual_revenue",
    "company_annual_revenue_clean",
    "company_description",
    "company_total_funding",
    "company_total_funding_clean",
    "company_technologies",
    "company_linkedin_uid",
    "company_founded_year",
    "company_domain",
    "company_phone",
    "company_street_address",
    "company_full_address",
    "company_state",
    "company_city",
    "company_country",
    "company_postal_code",
)

_SCRAPE_OUTPUT_TABLE_JS = r"""
var root = document.querySelector('#data-tracking-output-table');
if (!root) return null;
var table = root.querySelector('table');
if (!table) return null;
function cellText(td) {
    var inv = td.querySelector('[class*="InvalidValue"]');
    if (inv && inv.textContent && inv.textContent.toLowerCase().indexOf('null') >= 0) return '';
    var a = td.querySelector('a[href]');
    if (a) return (a.getAttribute('href') || '').trim();
    var sp = td.querySelector('span.innerText');
    if (sp) return (sp.textContent || '').trim();
    return (td.innerText || '').trim();
}
function thLabel(th) {
    var alt = th.querySelector('.column-header-label-alt');
    return ((alt ? alt.textContent : th.textContent) || '').trim();
}
/* Last thead row = leaf column headers (avoids multi-row header / group misalignment). */
function flattenHeaderCells(table) {
    var trs = table.querySelectorAll('thead tr');
    if (trs.length === 0) return [];
    var lastTr = trs[trs.length - 1];
    var headers = [];
    var ths = lastTr.querySelectorAll('th');
    for (var i = 0; i < ths.length; i++) {
        var th = ths[i];
        var label = thLabel(th);
        var span = parseInt(th.getAttribute('colspan') || '1', 10) || 1;
        for (var s = 0; s < span; s++) {
            headers.push(label);
        }
    }
    return headers;
}
function flattenDataCells(tr) {
    var cells = [];
    var tds = tr.querySelectorAll('td');
    for (var i = 0; i < tds.length; i++) {
        var td = tds[i];
        var span = parseInt(td.getAttribute('colspan') || '1', 10) || 1;
        var t = cellText(td);
        for (var s = 0; s < span; s++) {
            cells.push(t);
        }
    }
    return cells;
}
var headers = flattenHeaderCells(table);
var outRows = [];
var trs = table.querySelectorAll('tbody tr');
for (var r = 0; r < trs.length; r++) {
    var tr = trs[r];
    var cells = flattenDataCells(tr);
    if (cells.length === 0) continue;
    outRows.push(cells);
}
return { headers: headers, rows: outRows };
"""


def _detect_local_chrome_major() -> int | None:
    cmds = [
        ["google-chrome", "--version"],
        ["google-chrome-stable", "--version"],
        ["chromium-browser", "--version"],
        ["chromium", "--version"],
    ]
    for cmd in cmds:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=6, check=False)
        except Exception:
            continue
        raw = f"{proc.stdout}\n{proc.stderr}".strip()
        m = re.search(r"(\d+)\.\d+\.\d+\.\d+", raw)
        if not m:
            continue
        try:
            return int(m.group(1))
        except Exception:
            continue
    return None


# Origins used for Storage.clearDataForOrigin before a fresh signup/signin on a reused profile.
_APIFY_WEB_ORIGINS: tuple[str, ...] = (
    "https://console.apify.com",
    "https://apify.com",
)


def clear_chrome_browsing_data(driver) -> None:
    """
    Best-effort CDP wipe before signup/signin when reusing a on-disk Chrome profile.
    Does not remove the profile directory; clears cookies, cache, and Apify site storage.
    """
    try:
        driver.execute_cdp_cmd("Network.enable", {})
    except Exception:
        pass
    for cmd in ("Network.clearBrowserCookies", "Network.clearBrowserCache"):
        try:
            driver.execute_cdp_cmd(cmd, {})
        except Exception:
            pass
    storage_types = (
        "cookies,local_storage,indexeddb,cache_storage,service_workers,"
        "interest_groups,shared_storage,storage_buckets"
    )
    for origin in _APIFY_WEB_ORIGINS:
        try:
            driver.execute_cdp_cmd(
                "Storage.clearDataForOrigin",
                {"origin": origin, "storageTypes": storage_types},
            )
        except Exception:
            try:
                driver.execute_cdp_cmd(
                    "Storage.clearDataForOrigin",
                    {"origin": origin, "storageTypes": "all"},
                )
            except Exception:
                pass


def build_uc_driver(
    *,
    proxy_server: str | None = None,
    user_data_dir: str | None = None,
    profile_directory: str | None = None,
    clear_site_data_before_use: bool = False,
    headless: bool = False,
) -> uc.Chrome:
    chrome_major_detected = _detect_local_chrome_major()
    major = chrome_major_detected or 131
    fp = None
    if str(_LIB_DIR) not in sys.path:
        sys.path.insert(0, str(_LIB_DIR))
    try:
        from . import config as _apify_cfg

        if getattr(_apify_cfg, "BROWSER_FINGERPRINT", True):
            from browser_fingerprint import apply_fingerprint, pick_fingerprint

            fp = pick_fingerprint(chrome_major=major)
    except Exception:
        fp = None

    options = uc.ChromeOptions()
    options.add_argument("--disable-dev-shm-usage")
    if headless:
        options.add_argument("--headless=new")

    udd = (user_data_dir or "").strip()
    if udd:
        options.add_argument(f"--user-data-dir={udd}")
        pd = (profile_directory or "").strip()
        if pd:
            options.add_argument(f"--profile-directory={pd}")
        print(f"[browser] user-data-dir={udd}" + (f" profile-directory={pd}" if pd else ""))

    incognito_enabled = False
    try:
        from . import config as _apify_cfg2

        incognito_enabled = bool(getattr(_apify_cfg2, "INCOGNITO", False))
    except Exception:
        incognito_enabled = False
    if udd:
        if incognito_enabled:
            print("[browser] Skipping incognito (persistent user-data-dir in use)")
    elif incognito_enabled:
        options.add_argument("--incognito")
        print("[browser] Incognito mode enabled")
    # If you pass a proxy URL (http://..., https://..., socks5://...), Chrome will route traffic through it.
    if proxy_server:
        options.add_argument(f"--proxy-server={proxy_server}")
    if fp is not None:
        options.add_argument(f"--lang={fp.locale}")
        options.add_argument(f"--window-size={fp.viewport_width},{fp.viewport_height}")
        options.add_experimental_option(
            "prefs",
            {"intl.accept_languages": fp.accept_languages},
        )
        print(
            f"[browser] Profile: {fp.platform_label} | tz={fp.timezone_id} | "
            f"lang={fp.locale} | {fp.viewport_width}x{fp.viewport_height} @ {fp.device_scale_factor}x"
        )

    driver = uc.Chrome(
        options=options,
        use_subprocess=True,
        version_main=chrome_major_detected,
    )
    driver.implicitly_wait(2)
    driver.set_page_load_timeout(120)
    if fp is not None:
        try:
            apply_fingerprint(driver, fp)
        except Exception as exc:
            print(f"[browser] Fingerprint CDP partial failure (continuing): {exc}")
    reset_synthetic_mouse()
    if clear_site_data_before_use:
        try:
            clear_chrome_browsing_data(driver)
            print("[browser] Cleared cookies/cache and Apify site storage (CDP)")
        except Exception as exc:
            print(f"[browser] CDP storage clear partial failure (continuing): {exc}")
    return driver


def _sign_in_captcha_visible(driver) -> bool:
    markers = [
        (By.CSS_SELECTOR, "iframe[src*='recaptcha']"),
        (By.CSS_SELECTOR, "div.grecaptcha-badge"),
        (By.CSS_SELECTOR, "textarea#g-recaptcha-response"),
        (By.XPATH, "//*[contains(translate(., 'RECAPTCHA', 'recaptcha'), 'recaptcha')]"),
        (
            By.XPATH,
            "//*[contains(translate(., 'VERIFY YOU ARE HUMAN', 'verify you are human'), "
            "'verify you are human')]",
        ),
    ]
    for by, selector in markers:
        try:
            for el in driver.find_elements(by, selector):
                if el.is_displayed():
                    return True
        except Exception:
            continue
    return False


def _wait_sign_in_captcha_clear(driver, *, timeout_sec: int = 420) -> bool:
    """
    If captcha appears, wait for it to disappear so parallel sessions can resume
    without terminal Enter prompts.
    """
    if not _sign_in_captcha_visible(driver):
        return True
    print("[scrape] Captcha detected on sign-in; waiting for manual solve in browser...")
    end = time.time() + max(10, timeout_sec)
    while time.time() < end:
        checkpoint()
        if not _sign_in_captcha_visible(driver):
            print("[scrape] Captcha cleared; resuming sign-in flow.")
            return True
        _rng_pause(0.6, 1.2)
    print("[scrape] Captcha did not clear in time.")
    return False


def _find_first_clickable_sign_in(
    driver, selectors: list[tuple[By, str]], *, timeout: int = 20
):
    end = time.time() + timeout
    while time.time() < end:
        for by, selector in selectors:
            for el in driver.find_elements(by, selector):
                try:
                    if el.is_displayed() and el.is_enabled():
                        return el
                except Exception:
                    continue
        _rng_pause(0.15, 0.4)
    return None


def apify_sign_in(driver, email: str, password: str, *, timeout: int = 120) -> bool:
    """
    Apify console sign-in: email → Next → password → Sign in (same flow as signup email step).
    If reCAPTCHA appears, pause for manual solve (Enter), like auto-signup.
    """
    try:
        driver.switch_to.default_content()
    except Exception:
        pass
    try:
        driver.get(APIFY_SIGN_IN_URL)
    except TimeoutException:
        pass
    dismiss_intercom_precheck(driver)
    _rng_pause(0.8, 1.4)

    try:
        email_el = WebDriverWait(driver, min(30, timeout)).until(
            EC.visibility_of_element_located(
                (By.CSS_SELECTOR, "input[data-test='email'], input#email, input[type='email']")
            )
        )
    except TimeoutException:
        print("[scrape] Sign-in email field not found.")
        return False

    human_click_smooth(driver, email_el)
    try:
        email_el.clear()
    except Exception:
        pass
    email_el.send_keys(Keys.CONTROL, "a")
    email_el.send_keys(Keys.BACKSPACE)
    human_type_text_slow(email_el, email)
    _rng_pause(0.2, 0.4)

    next_btn = _find_first_clickable_sign_in(
        driver,
        [
            (By.XPATH, "//button[@type='submit' and normalize-space()='Next']"),
            (By.XPATH, "//button[normalize-space()='Next']"),
        ],
        timeout=15,
    )
    if not next_btn:
        print("[scrape] Sign-in Next button not found.")
        return False
    human_click_smooth(driver, next_btn)
    _rng_pause(0.35, 0.65)

    end_wait = time.time() + 28
    while time.time() < end_wait:
        checkpoint()
        if _sign_in_captcha_visible(driver):
            if not _wait_sign_in_captcha_clear(driver):
                return False
            break
        pws = driver.find_elements(
            By.CSS_SELECTOR,
            "input[data-test='password'], input#password, input[name='password'], input[type='password']",
        )
        if any(e.is_displayed() for e in pws):
            break
        _rng_pause(0.35, 0.7)

    if _sign_in_captcha_visible(driver) and not _wait_sign_in_captcha_clear(driver):
        return False

    try:
        pw_el = WebDriverWait(driver, timeout).until(
            EC.visibility_of_element_located(
                (
                    By.CSS_SELECTOR,
                    "input[data-test='password'], input#password, input[type='password']",
                )
            )
        )
    except TimeoutException:
        print("[scrape] Sign-in password field not found after Next.")
        return False

    human_click_smooth(driver, pw_el)
    try:
        pw_el.clear()
    except Exception:
        pass
    pw_el.send_keys(Keys.CONTROL, "a")
    pw_el.send_keys(Keys.BACKSPACE)
    human_type_text_slow(pw_el, password)
    _rng_pause(0.25, 0.45)

    submit = _find_first_clickable_sign_in(
        driver,
        [
            (By.CSS_SELECTOR, "button[data-test='submit-button']"),
            (By.XPATH, "//button[@type='submit' and contains(., 'Sign in')]"),
            (By.XPATH, "//button[contains(., 'Sign in')]"),
            (By.XPATH, "//button[contains(., 'Log in')]"),
        ],
        timeout=20,
    )
    if not submit:
        print("[scrape] Sign-in submit button not found.")
        return False
    human_click_smooth(driver, submit)
    _rng_pause(2.0, 3.5)

    if _sign_in_captcha_visible(driver):
        if not _wait_sign_in_captcha_clear(driver):
            return False
        _rng_pause(1.0, 2.0)

    _rng_pause(1.0, 2.0)
    return "sign-in" not in driver.current_url.lower()


def _ensure_top_window(driver) -> None:
    try:
        driver.switch_to.default_content()
    except Exception:
        pass


_INTERCOM_TOUR_DIALOG_XPATHS: tuple[str, ...] = (
    "//div[@role='dialog' and @aria-modal='true' and "
    "(contains(@aria-label, 'Tour') or contains(@aria-describedby, 'tour-step'))]",
    "//div[@role='dialog' and (contains(@aria-label, 'Tour step') or contains(@aria-describedby, 'tour-step-content'))]",
    "//div[@role='dialog' and .//*[contains(@class,'intercom')]]"
    "[.//button[@aria-label='Close']]"
    "[contains(@aria-label,'Tour') or contains(@aria-describedby,'tour-step')]",
)


def _try_dismiss_intercom_dialog_main(driver) -> bool:
    """Legacy top-document Intercom tour dialog: find Close and click once."""
    target = None
    try:
        for xp in _INTERCOM_TOUR_DIALOG_XPATHS:
            for d in driver.find_elements(By.XPATH, xp):
                try:
                    if d.is_displayed():
                        target = d
                        break
                except Exception:
                    continue
            if target is not None:
                break
    except Exception:
        pass
    if target is None:
        return False
    btn = None
    try:
        for b in target.find_elements(By.CSS_SELECTOR, "button[aria-label='Close']"):
            try:
                if b.is_displayed():
                    btn = b
                    break
            except Exception:
                continue
    except Exception:
        pass
    if btn is None:
        return False
    human_click_smooth(driver, btn)
    return True


def _dismiss_intercom_tour_via_positioner_iframe(driver) -> bool:
    """
    Newer Intercom tours render under #intercom-positioner-tree; the step UI is in
    iframe.intercom-tour-frame and Close is inside that iframe, not the top document.
    """
    try:
        trees = driver.find_elements(
            By.CSS_SELECTOR,
            "#intercom-positioner-tree.intercom-namespace, #intercom-positioner-tree",
        )
        if not trees or not any(t.is_displayed() for t in trees):
            return False
        frames = driver.find_elements(
            By.CSS_SELECTOR,
            "iframe.intercom-tour-frame, iframe[name='intercom-tour-frame']",
        )
        for fr in frames:
            try:
                if not fr.is_displayed():
                    continue
                driver.switch_to.frame(fr)
                try:
                    for b in driver.find_elements(By.CSS_SELECTOR, "button[aria-label='Close']"):
                        try:
                            if b.is_displayed():
                                human_click_smooth(driver, b)
                                return True
                        except Exception:
                            continue
                finally:
                    driver.switch_to.default_content()
            except Exception:
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass
    except Exception:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
    return False


def _try_dismiss_intercom_once(driver) -> bool:
    """
    One pass: top window, iframe tour Close or main-document tour dialog Close.
    Returns True if something was dismissed.
    """
    _ensure_top_window(driver)
    if _dismiss_intercom_tour_via_positioner_iframe(driver):
        return True
    return _try_dismiss_intercom_dialog_main(driver)


def dismiss_intercom_precheck(driver, *, max_passes: int = 4) -> None:
    """
    Fast pre-check before UI actions: dismiss visible Intercom tours without long polling.
    Repeats up to max_passes only while each pass closes a step (multi-step tours).
    If nothing is open, the first pass returns immediately.
    """
    for _ in range(max_passes):
        if not _try_dismiss_intercom_once(driver):
            break
        _rng_pause(0.2, 0.35)


def navigate_actor_input(driver) -> None:
    try:
        driver.get(APIFY_ACTOR_INPUT_URL)
    except TimeoutException:
        pass
    _rng_pause(1.5, 2.5)
    dismiss_intercom_precheck(driver)


def refresh_actor_input_page(driver) -> None:
    """
    Reload the actor input URL (or navigate there) so JSON tab / Monaco can appear after slow loads.
    """
    _ensure_top_window(driver)
    try:
        u = driver.current_url or ""
        if "console.apify.com" in u and APIFY_ACTOR_ID in u and "/input" in u:
            try:
                driver.refresh()
            except TimeoutException:
                pass
        else:
            navigate_actor_input(driver)
            return
    except Exception:
        navigate_actor_input(driver)
        return
    _rng_pause(1.2, 2.2)
    dismiss_intercom_precheck(driver)


def _actor_ui_remaining(deadline: float) -> float:
    return max(0.0, deadline - time.time())


def _actor_json_attempt_timeout(deadline: float, default: float) -> int:
    """Clamp WebDriver wait so we do not overshoot the outer resolve budget."""
    return int(max(5.0, min(float(default), _actor_ui_remaining(deadline))))


def prepare_actor_json_input(driver, filter_obj: dict[str, Any]) -> tuple[bool, str]:
    """
    Switch JSON tab and paste filter JSON into Monaco, **refreshing the page and retrying**
    until both succeed or ``ACTOR_INPUT_JSON_UI_RESOLVE_BUDGET_SEC`` is exhausted.
    Returns ``(True, "")`` or ``(False, "json_ui_timeout")``.
    """
    budget = float(
        getattr(_apify_cfg, "ACTOR_INPUT_JSON_UI_RESOLVE_BUDGET_SEC", 300.0)
    )
    budget = max(45.0, budget)
    per = float(
        getattr(_apify_cfg, "ACTOR_INPUT_JSON_UI_ATTEMPT_TIMEOUT_SEC", 25.0)
    )
    per = max(8.0, min(per, 60.0))
    deadline = time.time() + budget
    json_text = json.dumps(filter_obj, ensure_ascii=False, indent=2)
    round_n = 0
    while _actor_ui_remaining(deadline) > 3.0:
        round_n += 1
        t_json = _actor_json_attempt_timeout(deadline, per)
        if not click_json_tab(driver, timeout=t_json, verbose=False):
            print(
                f"[scrape] JSON tab not ready (round {round_n}); "
                f"refreshing (~{_actor_ui_remaining(deadline):.0f}s budget left)..."
            )
            refresh_actor_input_page(driver)
            continue
        t_paste = _actor_json_attempt_timeout(deadline, per)
        if not paste_json_into_editor(driver, json_text, timeout=t_paste, verbose=False):
            print(
                f"[scrape] Monaco / JSON editor not ready (round {round_n}); "
                f"refreshing (~{_actor_ui_remaining(deadline):.0f}s budget left)..."
            )
            refresh_actor_input_page(driver)
            continue
        if round_n > 1:
            print(f"[scrape] Actor JSON UI ready after {round_n} page attempt(s).")
        return True, ""
    print(
        f"[scrape] JSON tab / Monaco not available after {budget:.0f}s resolve budget."
    )
    return False, "json_ui_timeout"


def _click_json_tab_attempt(driver, *, timeout: int) -> bool:
    try:
        btns = WebDriverWait(driver, timeout).until(
            EC.presence_of_all_elements_located(
                (By.XPATH, "//button[contains(@class,'ButtonSwitch__Item')]//p[text()='JSON']/..")
            )
        )
    except TimeoutException:
        btns = driver.find_elements(
            By.XPATH,
            "//button[contains(@class,'ButtonSwitch__Item')][.//p[text()='JSON']]",
        )
    if not btns:
        return False
    for b in btns:
        if b.is_displayed():
            human_click_smooth(driver, b)
            _rng_pause(0.4, 0.9)
            return True
    return False


def click_json_tab(driver, *, timeout: int = 30, verbose: bool = True) -> bool:
    _ensure_top_window(driver)
    dismiss_intercom_precheck(driver)
    if _click_json_tab_attempt(driver, timeout=timeout):
        return True
    dismiss_intercom_precheck(driver)
    _rng_pause(0.2, 0.4)
    if _click_json_tab_attempt(driver, timeout=timeout):
        return True
    if verbose:
        print("[scrape] JSON tab switch not found.")
    return False


def _paste_via_clipboard(text: str) -> bool:
    try:
        import pyperclip  # type: ignore[import-untyped]

        pyperclip.copy(text)
        return True
    except Exception:
        return False


def _set_monaco_json_value(driver, json_text: str) -> bool:
    """
    Replace the full JSON in Apify's Monaco input in one shot (avoids hidden textarea / paste merge bugs).
    Falls back to keyboard paste if this returns False.
    """
    try:
        ok = driver.execute_script(
            """
            var text = arguments[0];
            var monaco = window.monaco;
            if (!monaco || !monaco.editor || typeof monaco.editor.getEditors !== 'function') {
                return false;
            }
            var editors = monaco.editor.getEditors();
            if (!editors || !editors.length) {
                return false;
            }
            var inputRoot = document.querySelector('#input');
            var ed = null;
            for (var i = 0; i < editors.length; i++) {
                var node = editors[i].getDomNode && editors[i].getDomNode();
                if (inputRoot && node && inputRoot.contains(node)) {
                    ed = editors[i];
                    break;
                }
            }
            if (!ed) {
                ed = editors[editors.length - 1];
            }
            if (!ed || typeof ed.setValue !== 'function') {
                return false;
            }
            ed.setValue(text);
            return true;
            """,
            json_text,
        )
        return bool(ok)
    except Exception:
        return False


def _paste_json_into_editor_attempt(
    driver, json_text: str, *, timeout: int, verbose: bool = True
) -> bool:
    try:
        ta = WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#input textarea.inputarea"))
        )
    except TimeoutException:
        return False

    if _set_monaco_json_value(driver, json_text):
        _rng_pause(0.35, 0.7)
        return True

    if verbose:
        print("[scrape] Monaco setValue unavailable; falling back to select-all + paste.")
    human_click_smooth(driver, ta)
    _sleep(0.22)
    (
        ActionChains(driver)
        .key_down(Keys.CONTROL)
        .send_keys("a")
        .key_up(Keys.CONTROL)
        .pause(0.12)
        .perform()
    )
    _sleep(0.1)
    ta.send_keys(Keys.DELETE)
    _sleep(0.12)
    if _paste_via_clipboard(json_text):
        (
            ActionChains(driver)
            .key_down(Keys.CONTROL)
            .send_keys("v")
            .key_up(Keys.CONTROL)
            .perform()
        )
    else:
        # Large JSON: bulk insert; slow per-char would take minutes.
        ta.send_keys(json_text)
    _rng_pause(0.5, 1.0)
    return True


def paste_json_into_editor(
    driver, json_text: str, *, timeout: int = 40, verbose: bool = True
) -> bool:
    _ensure_top_window(driver)
    dismiss_intercom_precheck(driver)
    if _paste_json_into_editor_attempt(
        driver, json_text, timeout=timeout, verbose=verbose
    ):
        return True
    dismiss_intercom_precheck(driver)
    _rng_pause(0.2, 0.4)
    if _paste_json_into_editor_attempt(
        driver, json_text, timeout=timeout, verbose=verbose
    ):
        return True
    if verbose:
        print("[scrape] Monaco textarea not found.")
    return False


def _click_save_and_start_attempt(driver, *, timeout: int) -> bool:
    for sel in (
        (By.CSS_SELECTOR, "button[data-test='actor-run-button']"),
        (By.CSS_SELECTOR, "#onboarding-run-actor"),
        (By.XPATH, "//button[contains(., 'Save') and contains(., 'Start')]"),
    ):
        try:
            el = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable(sel))
            human_click_smooth(driver, el)
            return True
        except TimeoutException:
            continue
    return False


def click_save_and_start(driver, *, timeout: int = 25) -> bool:
    _ensure_top_window(driver)
    dismiss_intercom_precheck(driver)
    if _click_save_and_start_attempt(driver, timeout=timeout):
        return True
    dismiss_intercom_precheck(driver)
    _rng_pause(0.2, 0.4)
    if _click_save_and_start_attempt(driver, timeout=timeout):
        return True
    print("[scrape] Save & Start not found.")
    return False


def wait_for_run_output_url(driver, *, timeout: int = 600) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        checkpoint()
        u = driver.current_url
        if "/runs/" in u and ("/actors/" in u or "/actor-runs/" in u):
            if "#output" not in u:
                try:
                    base = u.split("#")[0]
                    driver.get(base + "#output")
                except Exception:
                    pass
            _rng_pause(0.45, 0.9)
            return True
        _rng_pause(0.8, 1.5)
    return False


def read_actor_run_status_icon(driver) -> str | None:
    """
    Read Apify run state from the status badge (stable data-test), e.g. RUNNING, SUCCEEDED, FAILED.
    The icon element carries ``status="<STATE>"`` (see ActorRunStatusIcon).
    """
    try:
        raw = driver.execute_script(
            """
            var root = document.querySelector('[data-test="actor-job-summary"]');
            if (!root) root = document.body;
            var badge = root.querySelector('[data-test="actor-run-summary-status"]')
                || document.querySelector('[data-test="actor-run-summary-status"]');
            if (!badge) return null;
            var icon = badge.querySelector('[status]');
            if (!icon) return null;
            var s = icon.getAttribute('status');
            return s ? String(s).trim() : null;
            """
        )
        if raw is None:
            return None
        s = str(raw).strip()
        return s if s else None
    except Exception:
        return None


def wait_for_actor_run_not_running(
    driver, *, timeout: float | None = None
) -> tuple[bool, str | None]:
    """
    Block while the run status icon is RUNNING; proceed once it shows a terminal state
    (SUCCEEDED, FAILED, ABORTED, …). Returns (False, last_status) on timeout while RUNNING
    or if the status badge never appears.
    """
    cap = float(timeout if timeout is not None else WAIT_ACTOR_RUN_STATUS_SEC)
    cap = max(0.0, cap)
    if cap <= 0:
        return True, None

    end = time.time() + cap
    last: str | None = None

    while time.time() < end:
        checkpoint()
        st = read_actor_run_status_icon(driver)
        last = st
        if st is None:
            _table_pause(0.5, 1.0)
            continue
        if st.upper() == "RUNNING":
            _table_pause(0.6, 1.2)
            continue
        print(f"[scrape] Actor run left RUNNING — status={st}")
        return True, st

    if last is None:
        print(
            f"[scrape] Timed out ({cap:.0f}s) without reading actor run status "
            "(missing [data-test=actor-run-summary-status] / [status]?)."
        )
    elif last.upper() == "RUNNING":
        print(f"[scrape] Timed out ({cap:.0f}s) while actor run was still RUNNING.")
    return False, last


# Prefer stable hooks from Apify's dataset view; class hashes may change.
_DATASET_TABLE_SELECTORS: tuple[str, ...] = (
    "#data-tracking-output-table table.data-table",
    "#data-tracking-output-table table.paginated-table-data",
    "#data-tracking-output-table table",
)


def _find_dataset_table(driver):
    for sel in _DATASET_TABLE_SELECTORS:
        for el in driver.find_elements(By.CSS_SELECTOR, sel):
            try:
                if el.is_displayed():
                    return el
            except Exception:
                continue
    return None


def _scroll_dataset_table_into_view(driver) -> None:
    for by, sel in (
        (By.CSS_SELECTOR, "#data-tracking-output-table"),
        (By.CSS_SELECTOR, "[id='data-tracking-output-table']"),
    ):
        try:
            wrap = driver.find_element(by, sel)
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});",
                wrap,
            )
            return
        except Exception:
            continue


def _tbody_has_any_td_rows(driver) -> bool:
    """
    True when the output table has at least one tbody tr with a td.
    Does not inspect loading-row class — Apify may keep that row forever (hidden via CSS).
    """
    try:
        ok = driver.execute_script(
            """
            var root = document.querySelector('#data-tracking-output-table');
            if (!root) return false;
            var tb = root.querySelector('tbody');
            if (!tb) return false;
            var trs = tb.querySelectorAll('tr');
            for (var i = 0; i < trs.length; i++) {
                if (trs[i].querySelectorAll('td').length > 0) return true;
            }
            return false;
            """
        )
        if ok:
            return True
    except Exception:
        pass
    table = _find_dataset_table(driver)
    if not table:
        return False
    try:
        for tr in table.find_elements(By.CSS_SELECTOR, "tbody tr"):
            if tr.find_elements(By.TAG_NAME, "td"):
                return True
    except Exception:
        pass
    return False


def _output_shows_no_results(driver) -> bool:
    """Apify shows an explicit empty state (e.g. <h3>No results</h3>) — no rows to scrape."""
    try:
        for el in driver.find_elements(By.TAG_NAME, "h3"):
            if not el.is_displayed():
                continue
            if (el.text or "").strip().lower() == "no results":
                return True
    except Exception:
        pass
    return False


def wait_for_dataset_table(driver, *, timeout: int = 600) -> bool:
    """
    Poll until the output table exists and tbody has at least one td (any row).
    Does not depend on loading-row disappearing or cell-count heuristics.
    """
    end = time.time() + timeout
    _scroll_dataset_table_into_view(driver)
    while time.time() < end:
        checkpoint()
        if _output_shows_no_results(driver):
            print("[scrape] Output shows 'No results'; treating as empty dataset.")
            return False
        if _find_dataset_table(driver) is not None:
            _scroll_dataset_table_into_view(driver)
            if _tbody_has_any_td_rows(driver):
                return True
            if _output_shows_no_results(driver):
                print("[scrape] Output shows 'No results'; treating as empty dataset.")
                return False
        else:
            if _output_shows_no_results(driver):
                print("[scrape] Output shows 'No results'; treating as empty dataset.")
                return False
        _table_pause(0.35, 0.65)
    return False


_VIRTUAL_TABLE_SCROLL_ROUNDS = 20


def _expand_virtualized_table_rows(driver) -> None:
    """Scroll output table containers so virtualized DOM fills rows top-to-bottom before scrape."""
    try:
        driver.execute_script(
            """
            var root = document.querySelector('#data-tracking-output-table');
            if (!root) return;
            var rounds = arguments[0];
            for (var r = 0; r < rounds; r++) {
                var nodes = root.querySelectorAll('*');
                for (var i = 0; i < nodes.length; i++) {
                    var e = nodes[i];
                    try {
                        var st = window.getComputedStyle(e);
                        if ((st.overflowY === 'auto' || st.overflowY === 'scroll') &&
                            e.scrollHeight > e.clientHeight + 2) {
                            e.scrollTop = e.scrollHeight;
                        }
                    } catch (err) {}
                }
                var tb = root.querySelector('tbody');
                if (tb) try { tb.scrollTop = tb.scrollHeight; } catch (e2) {}
                var last = root.querySelector('tbody tr:last-child');
                if (last) try { last.scrollIntoView({block: 'end'}); } catch (e3) {}
            }
            """,
            _VIRTUAL_TABLE_SCROLL_ROUNDS,
        )
    except Exception:
        pass


def _aligned_row_to_snake_first_wins(headers: list[str], cells: list[Any]) -> dict[str, str]:
    """Map header+cell pairs left-to-right; first non-empty wins per snake_case key (no collisions)."""
    sm: dict[str, str] = {}
    n = min(len(headers), len(cells))
    for i in range(n):
        h = (headers[i] or "").strip()
        if not h or h in ("#", "# "):
            continue
        sk = _norm_header_snake(h)
        if sk == "keywords":
            continue
        val = str(cells[i] if cells[i] is not None else "").strip()
        if sk not in sm:
            sm[sk] = val
        elif not sm[sk] and val:
            sm[sk] = val
    return sm


def _normalize_work_email_key(sm: dict[str, str]) -> None:
    """Collapse work-email column variants into `email` for export (Apify labels vary slightly)."""
    if (sm.get("email") or "").strip():
        return
    for alt in ("work_email", "e_mail", "email_address", "work_email_address"):
        v = (sm.get(alt) or "").strip()
        if v:
            sm["email"] = v
            return


def _row_is_apify_loading_placeholder(sm: dict[str, str]) -> bool:
    """True when this tbody tr is the virtualized \"Loading results…\" placeholder, not a data row."""
    blob = " ".join(str(v or "").strip().lower() for v in sm.values())
    if not blob:
        return False
    markers = (
        "loading results",
        "it might take a few seconds",
        "take a few seconds to load",
        "take a few seconds to load the results",
    )
    return any(m in blob for m in markers)


def _row_keep_for_export(sm: dict[str, str]) -> bool:
    """Drop skeleton / loading rows; keep rows with a non-empty work email (after normalizing aliases)."""
    if _row_is_apify_loading_placeholder(sm):
        return False
    _normalize_work_email_key(sm)
    return bool((sm.get("email") or "").strip())


def scrape_current_page_rows(driver) -> tuple[list[str], list[dict[str, str]]]:
    """
    Scroll to load virtualized rows, then read table in document order: last thead row + colspan
    alignment, tbody tr top-to-bottom. Rows are snake_case dicts; duplicate header labels use first win.
    """
    _expand_virtualized_table_rows(driver)
    _table_pause(0.22, 0.38)
    _expand_virtualized_table_rows(driver)
    _table_pause(0.18, 0.32)
    _expand_virtualized_table_rows(driver)
    _table_pause(0.14, 0.26)

    try:
        _js = "return (function() {\n" + _SCRAPE_OUTPUT_TABLE_JS + "\n})();"
        data = driver.execute_script(_js)
    except Exception as e:
        raise NoSuchElementException("Dataset table script failed.") from e
    if not data or not isinstance(data, dict):
        raise NoSuchElementException("Dataset table not found for scraping.")
    headers = [str(h or "").strip() for h in (data.get("headers") or [])]
    raw_rows = data.get("rows") or []
    rows_out: list[dict[str, str]] = []
    for cells in raw_rows:
        if not isinstance(cells, list):
            continue
        sm = _aligned_row_to_snake_first_wins(headers, cells)
        if not _row_keep_for_export(sm):
            continue
        rows_out.append(sm)
    return headers, rows_out


def _wait_until_export_rows_or_timeout(driver, *, max_wait_sec: float) -> None:
    """
    After the dataset table exists, Apify may still show only loading placeholders or an empty
    virtualized viewport. Poll until scrape_current_page_rows returns at least one exportable row,
    the UI shows explicit \"No results\", or max_wait_sec elapses.
    """
    if max_wait_sec <= 0:
        return
    end = time.time() + max_wait_sec
    while time.time() < end:
        checkpoint()
        if _output_shows_no_results(driver):
            return
        _, rows = scrape_current_page_rows(driver)
        if rows:
            return
        _expand_virtualized_table_rows(driver)
        _table_pause(0.35, 0.65)


def go_to_next_page_if_any(driver, *, deadline: float | None = None) -> bool:
    """
    Click page-next if enabled. After click, briefly wait for tbody cells — no loading-row logic,
    no long spin. If `deadline` is set, do not block past it.
    """
    if deadline is not None and time.time() >= deadline:
        return False
    try:
        nxt = driver.find_element(By.CSS_SELECTOR, 'button[data-test="page-next-button"]')
    except Exception:
        return False
    if nxt.get_attribute("disabled"):
        return False
    human_click_smooth(driver, nxt)
    _scroll_dataset_table_into_view(driver)
    # Allow virtualized tbody to repopulate after page change (was 2.8s; too short for many runs).
    end = time.time() + 15.0
    if deadline is not None:
        end = min(end, deadline)
    while time.time() < end:
        checkpoint()
        if _tbody_has_any_td_rows(driver):
            break
        _table_pause(0.06, 0.14)
    if deadline is None or time.time() < deadline:
        _expand_virtualized_table_rows(driver)
        _table_pause(0.4, 0.7)
        _expand_virtualized_table_rows(driver)
        _table_pause(0.25, 0.45)
    return True


def scrape_usage_usd(driver) -> float | None:
    try:
        el = driver.find_element(By.CSS_SELECTOR, '[data-test="navigation-usage"]')
        text = el.text or ""
    except Exception:
        return None
    m = re.search(r"\$([\d.,]+)", text.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def sub_industry_from_filter(filter_obj: dict[str, Any]) -> str:
    """CSV `sub_industry`: always from filter JSON keywords (`keyword` or `keywords`), not table columns."""
    kw = filter_obj.get("keyword")
    if kw is None:
        kw = filter_obj.get("keywords")
    if isinstance(kw, list):
        return "; ".join(str(x).strip() for x in kw if str(x).strip())
    if isinstance(kw, str) and kw.strip():
        return kw.strip()
    return ""


def _norm_header_snake(label: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", (label or "").strip().lower())
    return s.strip("_")


def _row_snake_map(row: dict[str, str]) -> dict[str, str]:
    """Map UI labels to snake_case; drop Keywords; first key wins per snake (stable export)."""
    out: dict[str, str] = {}
    for k, v in row.items():
        sk = _norm_header_snake(k)
        if sk == "keywords":
            continue
        val = (v or "").strip()
        if sk not in out:
            out[sk] = val
        elif not out[sk] and val:
            out[sk] = val
    return out


def _export_body_columns(rows: list[dict[str, str]]) -> list[str]:
    """Fixed Apify column order, then any extra snake_case keys from rows (except keywords)."""
    extra: set[str] = set()
    base = set(APIFY_LEADS_BODY_COLUMNS)
    for r in rows:
        for sk in _row_snake_map(r):
            if sk not in base and sk != "keywords":
                extra.add(sk)
    return list(APIFY_LEADS_BODY_COLUMNS) + sorted(extra)


def _merge_csv_fieldnames(existing: list[str], preferred: list[str]) -> list[str]:
    merged = list(existing)
    for c in preferred:
        if c not in merged:
            merged.append(c)
    return merged


def append_rows_csv(
    path: Path,
    rows: list[dict[str, str]],
    *,
    filter_obj: dict[str, Any],
    filter_index: int,
) -> int:
    """
    Columns: sub_industry (from filter keyword/keywords) + APIFY_LEADS_BODY_COLUMNS order (+ extras),
    no Keywords column, no per-row validation — writes every scraped row.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    sub_industry = sub_industry_from_filter(filter_obj)
    fn = filter_obj.get("file_name", "")
    body_cols = _export_body_columns(rows)
    preferred = ["sub_industry"] + body_cols + ["filter_file_name", "filter_index"]

    existing: list[dict[str, str]] = []
    if path.is_file() and path.stat().st_size > 0:
        with path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            old_fields = list(reader.fieldnames or [])
            for row in reader:
                existing.append({k: (row.get(k) or "") for k in old_fields})
        fieldnames = _merge_csv_fieldnames(old_fields, preferred)
    else:
        fieldnames = preferred

    written = 0
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for er in existing:
            w.writerow({k: er.get(k, "") for k in fieldnames})
        for r in rows:
            sm = _row_snake_map(r)
            out = {c: "" for c in fieldnames}
            out["sub_industry"] = sub_industry
            out["filter_file_name"] = str(fn)
            out["filter_index"] = str(filter_index)
            for c in fieldnames:
                if c in ("sub_industry", "filter_file_name", "filter_index"):
                    continue
                out[c] = sm.get(c, "")
            w.writerow(out)
            written += 1
    return written


def run_actor_once_collect_scraped_rows(
    driver,
    filter_obj: dict[str, Any],
) -> tuple[bool, str, list[dict[str, str]], bool]:
    """
    From actor input page: JSON tab → paste ``filter_obj`` as JSON → Save & Start →
    wait for run → scrape dataset table (page 1 + optional page 2).

    Returns ``(ok, error_code, rows, skipped)``. When ``skipped`` is True (no table in time),
    ``ok`` is True and ``rows`` is empty; the caller should treat as a soft skip.
    """
    checkpoint()
    ok_ui, ui_err = prepare_actor_json_input(driver, filter_obj)
    if not ok_ui:
        return False, ui_err, [], False
    if not click_save_and_start(driver):
        return False, "run_button", [], False
    if not wait_for_run_output_url(driver, timeout=WAIT_RUN_OUTPUT_URL_SEC):
        return False, "run_url", [], False

    min_before_table = float(
        getattr(_apify_cfg, "MIN_WAIT_SEC_BEFORE_TABLE_DETECT", 10.0)
    )
    min_before_table = max(0.0, min_before_table)
    if min_before_table > 0:
        print(
            f"[scrape] Waiting {min_before_table:.1f}s (minimum) before detecting dataset table..."
        )
        _deadline_min = time.time() + min_before_table
        while time.time() < _deadline_min:
            pause_aware_sleep(min(0.2, max(0.0, _deadline_min - time.time())))

    max_run_status_wait = float(
        getattr(_apify_cfg, "MAX_WAIT_SEC_FOR_ACTOR_RUN_STATUS", WAIT_ACTOR_RUN_STATUS_SEC)
    )
    if max_run_status_wait > 0:
        print(
            "[scrape] Waiting until actor run status is not RUNNING "
            f"(max {max_run_status_wait:.0f}s, badge [data-test=actor-run-summary-status])..."
        )
        ok_status, _terminal = wait_for_actor_run_not_running(
            driver, timeout=max_run_status_wait
        )
        if not ok_status:
            return False, "run_status_timeout", [], False

    if not wait_for_dataset_table(driver, timeout=WAIT_DATASET_TABLE_SEC):
        print("[scrape] No dataset table / no rows in time; skipping this filter.")
        navigate_actor_input(driver)
        return True, "", [], True

    follow_export = float(
        getattr(_apify_cfg, "MAX_WAIT_SEC_FOR_EXPORT_ROWS_AFTER_TABLE", 45.0)
    )
    if follow_export > 0:
        print(
            f"[scrape] Waiting up to {follow_export:.1f}s for exportable rows (non-loading, with email)..."
        )
        _wait_until_export_rows_or_timeout(driver, max_wait_sec=follow_export)

    _table_pause(0.28, 0.48)

    scrape_budget = float(
        getattr(_apify_cfg, "SCRAPE_TWO_PAGES_BUDGET_SEC", SCRAPE_TWO_PAGES_BUDGET_SEC)
    )
    scrape_budget = max(5.0, scrape_budget)
    scrape_deadline = time.time() + scrape_budget
    all_rows: list[dict[str, str]] = []
    _, page1 = scrape_current_page_rows(driver)
    all_rows.extend(page1)
    if time.time() < scrape_deadline and go_to_next_page_if_any(driver, deadline=scrape_deadline):
        if time.time() <= scrape_deadline:
            _, page2 = scrape_current_page_rows(driver)
            all_rows.extend(page2)

    return True, "", all_rows, False


def run_one_apify_search(
    driver,
    filter_obj: dict[str, Any],
    filter_index: int,
    csv_path: Path,
) -> dict[str, Any]:
    """
    From actor input page: JSON tab → paste → Save & Start → scrape table (page 1–2) → append CSV.
    JSON tab and Monaco editor are resolved with **refresh-and-retry** until
    ``ACTOR_INPUT_JSON_UI_RESOLVE_BUDGET_SEC`` (config) elapses, so transient UI misses
    do not stop the search after signup.
    After the run output URL loads, waits MIN_WAIT_SEC_BEFORE_TABLE_DETECT (config), then waits until
    the actor run badge is not RUNNING (MAX_WAIT_SEC_FOR_ACTOR_RUN_STATUS), then polls for the table.
    Scraping page 1 + pagination + page 2 is bounded by SCRAPE_TWO_PAGES_BUDGET_SEC.
    If the run finishes but no dataset table / rows appear in time, returns ok=True with skipped=True
    and navigates back to the actor input URL before returning.
    """
    ok, err, all_rows, skipped = run_actor_once_collect_scraped_rows(driver, filter_obj)
    if not ok:
        return {"ok": False, "error": err, "written_rows": 0}
    if skipped:
        usage = scrape_usage_usd(driver)
        return {
            "ok": True,
            "written_rows": 0,
            "skipped": True,
            "skip_reason": "no_table",
            "usage_usd": usage,
            "raw_row_count": 0,
        }

    n = append_rows_csv(
        csv_path,
        all_rows,
        filter_obj=filter_obj,
        filter_index=filter_index,
    )
    usage = scrape_usage_usd(driver)
    return {
        "ok": True,
        "written_rows": n,
        "usage_usd": usage,
        "raw_row_count": len(all_rows),
        "skipped": False,
    }
