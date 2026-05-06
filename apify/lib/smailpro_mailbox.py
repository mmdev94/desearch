"""
Smailpro / Sonjj temp **Outlook** via **browser UI** (same Selenium session as Apify).

Flow: ``my.sonjj.com/login`` → ``smailpro.com/temporary-email`` → wait/reload until header **user** menu → **Sign in**
(Smailpro often stays “Guest” until this) → reload temp page → Create / Generate via JS click (sticky ``#menu``) →
a **random** Smailpro pattern (``random@…``, ``random[real]@…``, ``…-2`` variants) on a **random** allowed domain →
Generate → brief wait (2–3s) for sidebar → read first Outlook address → Apify signup → poll inbox for verify link.

Credentials (never commit): ``SONJJ_ACCOUNT_EMAIL`` and ``SONJJ_ACCOUNT_PASSWORD`` in ``apify/.env`` or repo ``.env``.
"""
from __future__ import annotations

import os
import random
import re
import time
from pathlib import Path
from typing import Tuple

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

SONJJ_LOGIN_URL = "https://my.sonjj.com/login"
SMAILPRO_TEMP_URL = "https://smailpro.com/temporary-email"
OUTLOOK_PATTERN = re.compile(
    r"[A-Za-z0-9._%+\[\]]+@(outlook\.(com|kr|fr|com\.vn|co\.id|co\.th|com\.ar|co\.il)|hotmail\.com)(?:-2)?",
    re.I,
)

# Domains offered in Smailpro “Create temporary email” (random pick per generation).
_SMAILPRO_OUTLOOK_DOMAINS: tuple[str, ...] = (
    "hotmail.com",
    "outlook.com",
    "outlook.kr",
    "outlook.fr",
    "outlook.co.id",
    "outlook.co.th",
    "outlook.co.il",
)


def _pick_smailpro_outlook_pattern() -> str:
    """
    Random Smailpro template: local part style × domain (optional ``-2`` suffix on domain in UI).

    Styles: ``random@domain``, ``random[real]@domain``, ``random@domain-2``, ``random[real]@domain-2``.
    """
    domain = random.choice(_SMAILPRO_OUTLOOK_DOMAINS)
    style = random.randrange(4)
    if style == 0:
        return f"random@{domain}"
    if style == 1:
        return f"random[real]@{domain}"
    if style == 2:
        return f"random@{domain}-2"
    return f"random[real]@{domain}-2"


def _load_dotenv_apify() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    apify_dir = Path(__file__).resolve().parents[1]
    repo_root = apify_dir.parent
    for p in (apify_dir / ".env", repo_root / ".env"):
        if p.is_file():
            load_dotenv(p, override=False)


def _account_creds_from_env() -> tuple[str, str]:
    _load_dotenv_apify()
    email = (
        os.environ.get("SONJJ_ACCOUNT_EMAIL")
        or os.environ.get("SONJI_ACCOUNT_EMAIL")
        or ""
    ).strip()
    password = (
        os.environ.get("SONJJ_ACCOUNT_PASSWORD")
        or os.environ.get("SONJI_ACCOUNT_PASSWORD")
        or ""
    ).strip()
    if not email or not password:
        raise RuntimeError(
            "Set SONJJ_ACCOUNT_EMAIL and SONJJ_ACCOUNT_PASSWORD in apify/.env or repo .env "
            "(Sonjj / Smailpro web login)."
        )
    return email, password


def extract_apify_verify_link_from_body(blob: str) -> str | None:
    """Heuristics for Apify verification email HTML/text."""
    if not blob:
        return None
    m = re.search(
        r'href="(https://console\.apify\.com/verify-email/[^"]+)"[^>]*>\s*'
        r'(?:<span[^>]*>\s*</span>\s*)*'
        r'<span[^>]*>\s*Verify email address\s*</span>',
        blob,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if m:
        return m.group(1)
    m2 = re.search(
        r"https://console\.apify\.com/verify-email/[A-Za-z0-9_-]+",
        blob,
        flags=re.IGNORECASE,
    )
    if m2:
        return m2.group(0)
    return None


def extract_serper_verify_link_from_body(blob: str) -> str | None:
    """Heuristics for Serper verification email HTML/text."""
    if not blob:
        return None
    m = re.search(
        r"https://serper\.dev/confirm-email\?token=[A-Za-z0-9]+",
        blob,
        flags=re.IGNORECASE,
    )
    if m:
        return m.group(0)
    return None


def _switch_to_window_other_than(driver, avoid: str) -> str | None:
    for h in driver.window_handles:
        if h != avoid:
            driver.switch_to.window(h)
            return h
    return None


def _close_smailpro_sonjj_tabs(driver, main_handle: str) -> None:
    """Avoid stacking mail tabs when auto-signup retries after domain blacklist."""
    for h in list(driver.window_handles):
        if h == main_handle:
            continue
        driver.switch_to.window(h)
        try:
            url = driver.current_url or ""
        except Exception:
            url = ""
        if "smailpro.com" in url or "my.sonjj.com" in url:
            try:
                driver.close()
            except Exception:
                pass
    if main_handle in driver.window_handles:
        driver.switch_to.window(main_handle)


def _find_window_by_url_contains(driver, needle: str) -> str | None:
    cur = driver.current_window_handle
    for h in driver.window_handles:
        driver.switch_to.window(h)
        try:
            if needle in (driver.current_url or ""):
                return h
        except Exception:
            continue
    driver.switch_to.window(cur)
    return None


def _wait_login_success(driver, *, timeout: int = 120) -> None:
    end = time.time() + timeout
    warned = False
    while time.time() < end:
        try:
            url = driver.current_url or ""
        except Exception:
            url = ""
        if "my.sonjj.com/login" not in url:
            return
        if not warned and time.time() > end - timeout + 25:
            warned = True
            print(
                "[Smailpro/UI] Still on Sonjj login — solve captcha in the browser if shown, "
                "or check SONJJ_ACCOUNT_EMAIL / SONJJ_ACCOUNT_PASSWORD."
            )
        time.sleep(0.5)
    raise RuntimeError("Sonjj login did not leave /login in time (captcha or wrong password?).")


def _sonjj_login(driver) -> None:
    email, password = _account_creds_from_env()
    driver.get(SONJJ_LOGIN_URL)
    wait = WebDriverWait(driver, 45)
    em = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "input#email")))
    em.clear()
    em.send_keys(email)
    pw = driver.find_element(By.CSS_SELECTOR, "input#password")
    pw.clear()
    pw.send_keys(password)
    try:
        remember = driver.find_element(By.CSS_SELECTOR, "input#remember_me")
        if remember.is_displayed() and not remember.is_selected():
            remember.click()
    except Exception:
        pass
    login_btn = WebDriverWait(driver, 15).until(
        EC.element_to_be_clickable(
            (
                By.CSS_SELECTOR,
                "form#login button.g-recaptcha, form#login button[type='button'].button_primary_mod",
            )
        )
    )
    login_btn.click()
    _wait_login_success(driver, timeout=180)


def _scroll_center_and_js_click(driver, el) -> None:
    """Avoid sticky ``#menu`` header intercepting native clicks on lower buttons."""
    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});",
            el,
        )
        time.sleep(0.3)
    except Exception:
        pass
    driver.execute_script("arguments[0].click();", el)


def _wait_user_menu_button(driver, *, total_timeout: float = 120.0):
    """
    After ``my.sonjj.com`` login, Smailpro can load slowly; the header user button may appear late.
    Refresh / re-navigate until ``btn user`` is clickable or timeout.
    """
    end = time.time() + total_timeout
    attempt = 0
    while time.time() < end:
        attempt += 1
        wait_sec = min(20.0, max(4.0, end - time.time()))
        try:
            btn = WebDriverWait(driver, wait_sec).until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, "button[aria-label='btn user']")
                )
            )
            _scroll_center_and_js_click(driver, btn)
            time.sleep(0.65)
            return
        except TimeoutException:
            pass
        except Exception:
            pass
        if time.time() >= end:
            break
        print(
            f"[Smailpro/UI] User menu button not ready (attempt {attempt}); "
            "reloading temporary-email (slow network)..."
        )
        try:
            driver.get(SMAILPRO_TEMP_URL)
        except Exception:
            try:
                driver.refresh()
            except Exception:
                pass
        time.sleep(random.uniform(2.0, 4.0))
        try:
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except Exception:
            pass

    raise RuntimeError(
        "Smailpro: header user button (aria-label='btn user') not found after reloads. "
        "Try again or check Sonjj login / network."
    )


def _smailpro_sync_session_via_user_menu(driver) -> None:
    """
    Smailpro often shows ``Hi! Guest`` until the header user menu is used: open menu → **Sign in**
    (uses existing Sonjj cookies), then reload the temp-mail page.
    """
    print("[Smailpro/UI] Syncing Smailpro session (user menu → Sign in)...")
    try:
        _wait_user_menu_button(driver, total_timeout=120.0)
    except Exception as ex:
        print(f"[Smailpro/UI] Could not open user menu: {ex}")
        return

    sign_in_el = None
    for xp in (
        "//span[contains(@class,'cursor-pointer')][normalize-space()='Sign in']",
        "//li[contains(@class,'bg-gray-300')]//span[normalize-space()='Sign in']",
        "//*[normalize-space()='Sign in' and contains(@class,'cursor-pointer')]",
    ):
        try:
            for el in driver.find_elements(By.XPATH, xp):
                try:
                    if el.is_displayed():
                        sign_in_el = el
                        break
                except Exception:
                    continue
            if sign_in_el:
                break
        except Exception:
            continue

    if not sign_in_el:
        print("[Smailpro/UI] No Sign in in user menu — treating as already linked.")
        try:
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        except Exception:
            pass
        return

    try:
        _scroll_center_and_js_click(driver, sign_in_el)
    except Exception:
        try:
            sign_in_el.click()
        except Exception:
            pass
    time.sleep(2.0)

    end = time.time() + 45
    while time.time() < end:
        try:
            url = driver.current_url or ""
        except Exception:
            url = ""
        if "smailpro.com" in url and "temporary-email" in url:
            break
        time.sleep(0.35)

    try:
        driver.get(SMAILPRO_TEMP_URL)
    except Exception:
        pass
    WebDriverWait(driver, 35).until(
        EC.presence_of_element_located((By.TAG_NAME, "body"))
    )
    time.sleep(1.0)
    print("[Smailpro/UI] Reloaded temporary-email after Sign in.")


def _load_smailpro_temp_email_and_sync(driver) -> None:
    """Navigate to temp email page and run Smailpro’s user-menu Sign-in handshake."""
    driver.get(SMAILPRO_TEMP_URL)
    WebDriverWait(driver, 45).until(
        EC.presence_of_element_located((By.TAG_NAME, "body"))
    )
    time.sleep(random.uniform(1.0, 2.0))
    _smailpro_sync_session_via_user_menu(driver)


def _create_button_locator() -> Tuple[By, str]:
    return (
        By.XPATH,
        "//button[contains(@class,'bg-green-600') and .//span[normalize-space()='Create']]",
    )


def _load_smailpro_temp_email_light_for_retry(driver) -> None:
    """
    After Sonjj is already linked, avoid the long ``_wait_user_menu_button`` path when
    **Create** is already on the page. Only opens user menu → Sign in if Create is missing.
    """
    driver.get(SMAILPRO_TEMP_URL)
    WebDriverWait(driver, 45).until(
        EC.presence_of_element_located((By.TAG_NAME, "body"))
    )
    time.sleep(random.uniform(0.8, 1.4))
    by, sel = _create_button_locator()
    try:
        WebDriverWait(driver, 14).until(EC.element_to_be_clickable((by, sel)))
        return
    except TimeoutException:
        pass

    print("[Smailpro/UI] Create not ready quickly; trying user menu → Sign in if present...")
    try:
        btn = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "button[aria-label='btn user']")
            )
        )
        _scroll_center_and_js_click(driver, btn)
        time.sleep(0.45)
    except TimeoutException:
        print("[Smailpro/UI] User menu not found; one refresh of temporary-email.")
        try:
            driver.refresh()
        except Exception:
            pass
        WebDriverWait(driver, 25).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        time.sleep(1.2)
        return

    sign_in_el = None
    for xp in (
        "//span[contains(@class,'cursor-pointer')][normalize-space()='Sign in']",
        "//li[contains(@class,'bg-gray-300')]//span[normalize-space()='Sign in']",
        "//*[normalize-space()='Sign in' and contains(@class,'cursor-pointer')]",
    ):
        try:
            for el in driver.find_elements(By.XPATH, xp):
                try:
                    if el.is_displayed():
                        sign_in_el = el
                        break
                except Exception:
                    continue
            if sign_in_el:
                break
        except Exception:
            continue

    if sign_in_el:
        try:
            _scroll_center_and_js_click(driver, sign_in_el)
        except Exception:
            try:
                sign_in_el.click()
            except Exception:
                pass
        time.sleep(1.5)
        try:
            driver.get(SMAILPRO_TEMP_URL)
        except Exception:
            pass
        WebDriverWait(driver, 35).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        time.sleep(random.uniform(0.8, 1.2))
    else:
        try:
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        except Exception:
            pass


def _ensure_smailpro_work_window(driver, main_handle: str) -> None:
    """Focus a Smailpro tab, or open ``temporary-email`` in a new tab from ``main_handle``."""
    h = _find_window_by_url_contains(driver, "smailpro.com")
    if h:
        driver.switch_to.window(h)
        return
    driver.switch_to.window(main_handle)
    driver.execute_script("window.open(arguments[0], '_blank');", SMAILPRO_TEMP_URL)
    WebDriverWait(driver, 25).until(lambda d: len(d.window_handles) > 1)
    _switch_to_window_other_than(driver, main_handle)


def _click_create_and_generate_outlook(driver) -> None:
    wait = WebDriverWait(driver, 45)
    create_btn = wait.until(EC.element_to_be_clickable(_create_button_locator()))
    _scroll_center_and_js_click(driver, create_btn)
    wait.until(
        EC.visibility_of_element_located(
            (By.XPATH, "//h3[contains(.,'Create temporary email')]")
        )
    )
    time.sleep(0.4)
    # Email pattern field (placeholder contains random@)
    email_input = WebDriverWait(driver, 15).until(
        EC.visibility_of_element_located(
            (
                By.XPATH,
                "//div[contains(@class,'max-w-3xl')]//label[normalize-space()='Email']/following::input[@type='text'][1]",
            )
        )
    )
    want = _pick_smailpro_outlook_pattern()
    print(f"[Smailpro/UI] email pattern field: {want}")
    cur = (email_input.get_attribute("value") or "").strip()
    if cur != want:
        email_input.clear()
        email_input.send_keys(want)
        time.sleep(0.6)

    gen = WebDriverWait(driver, 25).until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                "//div[contains(@class,'max-w-3xl')]//button[contains(@class,'bg-blue-500') and .//div[normalize-space()='Generate']]",
            )
        )
    )
    # Native .click() hits sticky ``header#menu``; JS click after scroll avoids interception.
    _scroll_center_and_js_click(driver, gen)
    # Smailpro sidebar can lag behind a fast-closing modal; give the UI time to register the new inbox.
    time.sleep(random.uniform(2.0, 2.8))
    # Wait until modal closes or list shows new address
    end = time.time() + 90
    while time.time() < end:
        try:
            modals = driver.find_elements(
                By.XPATH, "//h3[contains(.,'Create temporary email')]"
            )
            visible = any(
                m.is_displayed() for m in modals
            )
            if not visible:
                break
        except Exception:
            break
        time.sleep(0.5)
    # Left sidebar often lags the modal closing; wait before scraping the new Outlook row.
    time.sleep(random.uniform(2.0, 3.0))


def _first_outlook_address_from_sidebar(driver) -> str | None:
    """First list row labeled Outlook — ``div.font-semibold`` email text."""
    rows = driver.find_elements(
        By.XPATH,
        "//li[.//span[contains(.,'Outlook')]]//div[contains(@class,'font-semibold')]",
    )
    for el in rows:
        try:
            t = (el.text or "").strip()
            if OUTLOOK_PATTERN.search(t):
                return t
        except Exception:
            continue
    for el in rows:
        try:
            t = (el.text or "").strip()
            if "@" in t:
                return t
        except Exception:
            continue
    return None


def _read_outlook_address_after_generate(driver, *, poll_seconds: float = 18.0) -> str | None:
    """Poll sidebar briefly after Generate — list can lag the modal closing."""
    end = time.time() + max(5.0, poll_seconds)
    while time.time() < end:
        addr = _first_outlook_address_from_sidebar(driver)
        if addr:
            return addr
        time.sleep(0.45)
    return None


def create_inbox_with_rotation(
    *,
    driver,
    apify_root: Path,
    usage_file: Path | None = None,
    reuse_mail_session: bool = False,
) -> tuple[str, str, None]:
    """
    Create a temp Outlook on Smailpro UI, return address.

    - ``reuse_mail_session=False`` (default): close prior Smailpro/Sonjj tabs, Sonjj login tab,
      full temp-email + user-menu sync, then Create / Generate.
    - ``reuse_mail_session=True``: keep existing tabs; focus Smailpro (or open temp-email),
      light load (no Sonjj login), then Create / Generate — for Apify retries after **email taken**.

    Returns ``(email, mailbox_id, None)`` — third value kept for call-site compatibility (no HTTP client).

    ``usage_file`` is ignored (legacy API quota); pass ``None``.
    """
    _ = apify_root
    _ = usage_file
    main_handle = driver.current_window_handle
    addr: str | None = None

    if reuse_mail_session:
        try:
            _ensure_smailpro_work_window(driver, main_handle)
            _load_smailpro_temp_email_light_for_retry(driver)
            for attempt in range(1, 4):
                _click_create_and_generate_outlook(driver)
                addr = _read_outlook_address_after_generate(driver, poll_seconds=22.0)
                if addr:
                    break
                print(
                    "[Smailpro/UI] Sidebar did not show Outlook after Generate; "
                    f"retrying Create flow (attempt {attempt}/3)."
                )
                try:
                    driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
                except Exception:
                    pass
                time.sleep(random.uniform(0.6, 1.1))
            if not addr:
                raise RuntimeError(
                    "Could not read a new Outlook address from Smailpro sidebar after Generate "
                    "(retried Create/Generate, reuse session)."
                )
            print(f"[Smailpro/UI] temp Outlook inbox: address={addr}")
        finally:
            driver.switch_to.window(main_handle)
        return addr, addr, None

    _close_smailpro_sonjj_tabs(driver, main_handle)
    driver.execute_script("window.open(arguments[0], '_blank');", SONJJ_LOGIN_URL)
    WebDriverWait(driver, 20).until(lambda d: len(d.window_handles) > 1)
    _switch_to_window_other_than(driver, main_handle)

    try:
        _sonjj_login(driver)
    except Exception:
        try:
            driver.close()
        except Exception:
            pass
        driver.switch_to.window(main_handle)
        raise

    try:
        _load_smailpro_temp_email_and_sync(driver)
        for attempt in range(1, 4):
            _click_create_and_generate_outlook(driver)
            addr = _read_outlook_address_after_generate(driver, poll_seconds=22.0)
            if addr:
                break
            print(
                "[Smailpro/UI] Sidebar did not show Outlook after Generate; "
                f"retrying Create flow (attempt {attempt}/3)."
            )
            try:
                driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            except Exception:
                pass
            time.sleep(random.uniform(0.6, 1.1))
        if not addr:
            raise RuntimeError(
                "Could not read a new Outlook address from Smailpro sidebar after Generate "
                "(retried Create/Generate)."
            )
        print(f"[Smailpro/UI] temp Outlook inbox: address={addr}")
    except Exception:
        try:
            driver.close()
        except Exception:
            pass
        driver.switch_to.window(main_handle)
        raise

    driver.switch_to.window(main_handle)
    return addr, addr, None


def _click_sidebar_row_for_email(driver, email: str) -> None:
    """Select the list row whose primary line matches ``email``."""
    for li in driver.find_elements(
        By.XPATH,
        "//li[.//span[contains(.,'Outlook')] or .//span[contains(.,'Microsoft')]]",
    ):
        try:
            for div in li.find_elements(By.CSS_SELECTOR, "div.font-semibold"):
                if (div.text or "").strip() == email.strip():
                    driver.execute_script("arguments[0].click();", li)
                    return
        except Exception:
            continue
    for li in driver.find_elements(By.XPATH, "//li[.//span[contains(.,'Outlook')]]"):
        try:
            for div in li.find_elements(By.CSS_SELECTOR, "div[class*='font-semibold']"):
                t = (div.text or "").strip()
                if t == email.strip() or email.split("@")[0][:8] in t:
                    driver.execute_script("arguments[0].click();", li)
                    return
        except Exception:
            continue


def find_apify_verification_link(
    driver,
    email: str,
    *,
    timeout_seconds: int = 240,
    poll_seconds: float = 3.0,
) -> str | None:
    """
    On the Smailpro tab (URL contains ``smailpro.com``), select the inbox, refresh, open Apify message, parse link from iframe ``srcdoc``.
    """
    main_handle = driver.current_window_handle
    sm_handle = _find_window_by_url_contains(driver, "smailpro.com")
    if not sm_handle:
        sm_handle = _find_window_by_url_contains(driver, "my.sonjj.com")
    if not sm_handle:
        print("[Smailpro/UI] No Smailpro tab found; open temporary-email in a second tab.")
        return None

    driver.switch_to.window(sm_handle)
    try:
        cur = driver.current_url or ""
    except Exception:
        cur = ""
    if "temporary-email" not in cur or "smailpro.com" not in cur:
        try:
            _load_smailpro_temp_email_and_sync(driver)
        except Exception:
            try:
                driver.get(SMAILPRO_TEMP_URL)
                time.sleep(1.2)
            except Exception:
                pass
    else:
        _smailpro_sync_session_via_user_menu(driver)

    end = time.time() + timeout_seconds
    while time.time() < end:
        try:
            _click_sidebar_row_for_email(driver, email)

            for sel in (
                (By.CSS_SELECTOR, "button#refresh"),
                (By.CSS_SELECTOR, "[aria-label='refresh']"),
                (By.XPATH, "//button[@title='refresh']"),
            ):
                try:
                    ref = driver.find_element(*sel)
                    if ref.is_displayed():
                        driver.execute_script("arguments[0].click();", ref)
                        break
                except Exception:
                    continue

            time.sleep(max(1.0, poll_seconds * 0.5))

            apify_row = None
            for xp in (
                "//h3[contains(.,'Verify your email address for Apify')]",
                "//*[contains(.,'Verify your email address for Apify')]",
            ):
                try:
                    for h3 in driver.find_elements(By.XPATH, xp):
                        if not h3.is_displayed():
                            continue
                        apify_row = h3.find_element(
                            By.XPATH,
                            "./ancestor::div[contains(@class,'cursor-pointer')][1]",
                        )
                        break
                    if apify_row:
                        break
                except Exception:
                    continue
            if apify_row:
                driver.execute_script("arguments[0].click();", apify_row)
                time.sleep(0.9)
                iframes = driver.find_elements(By.CSS_SELECTOR, "iframe[srcdoc]")
                for fr in iframes:
                    srcdoc = fr.get_attribute("srcdoc") or ""
                    link = extract_apify_verify_link_from_body(srcdoc)
                    if link:
                        print("[Smailpro/UI] Apify verify link found in message iframe.")
                        driver.switch_to.window(main_handle)
                        return link
                try:
                    blob = driver.page_source
                    link = extract_apify_verify_link_from_body(blob)
                    if link:
                        driver.switch_to.window(main_handle)
                        return link
                except Exception:
                    pass
        except Exception as ex:
            print(f"[Smailpro/UI] poll: {type(ex).__name__}: {ex}")

        time.sleep(poll_seconds * 0.75)

    try:
        driver.switch_to.window(main_handle)
    except Exception:
        pass
    return None


def find_serper_verification_link(
    driver,
    email: str,
    *,
    timeout_seconds: int = 240,
    poll_seconds: float = 3.0,
) -> str | None:
    """
    On the Smailpro tab, select inbox, refresh, open Serper message, parse confirm link.

    Expected: from support@serper.dev, subject "Please verify your email address".
    """
    main_handle = driver.current_window_handle
    sm_handle = _find_window_by_url_contains(driver, "smailpro.com")
    if not sm_handle:
        sm_handle = _find_window_by_url_contains(driver, "my.sonjj.com")
    if not sm_handle:
        print("[Smailpro/UI] No Smailpro tab found; open temporary-email in a second tab.")
        return None

    driver.switch_to.window(sm_handle)
    try:
        cur = driver.current_url or ""
    except Exception:
        cur = ""
    if "temporary-email" not in cur or "smailpro.com" not in cur:
        try:
            _load_smailpro_temp_email_and_sync(driver)
        except Exception:
            try:
                driver.get(SMAILPRO_TEMP_URL)
                time.sleep(1.2)
            except Exception:
                pass
    else:
        _smailpro_sync_session_via_user_menu(driver)

    end = time.time() + timeout_seconds
    while time.time() < end:
        try:
            _click_sidebar_row_for_email(driver, email)

            for sel in (
                (By.CSS_SELECTOR, "button#refresh"),
                (By.CSS_SELECTOR, "[aria-label='refresh']"),
                (By.XPATH, "//button[@title='refresh']"),
            ):
                try:
                    ref = driver.find_element(*sel)
                    if ref.is_displayed():
                        driver.execute_script("arguments[0].click();", ref)
                        break
                except Exception:
                    continue

            time.sleep(max(1.0, poll_seconds * 0.5))

            serper_row = None
            for xp in (
                "//h3[contains(.,'Please verify your email address')]",
                "//*[contains(.,'Please verify your email address')]",
            ):
                try:
                    for h3 in driver.find_elements(By.XPATH, xp):
                        if not h3.is_displayed():
                            continue
                        serper_row = h3.find_element(
                            By.XPATH,
                            "./ancestor::div[contains(@class,'cursor-pointer')][1]",
                        )
                        break
                    if serper_row:
                        break
                except Exception:
                    continue

            if serper_row:
                driver.execute_script("arguments[0].click();", serper_row)
                time.sleep(0.9)
                iframes = driver.find_elements(By.CSS_SELECTOR, "iframe[srcdoc]")
                for fr in iframes:
                    srcdoc = fr.get_attribute("srcdoc") or ""
                    link = extract_serper_verify_link_from_body(srcdoc)
                    if link:
                        print("[Smailpro/UI] Serper verify link found in message iframe.")
                        driver.switch_to.window(main_handle)
                        return link
                try:
                    blob = driver.page_source
                    link = extract_serper_verify_link_from_body(blob)
                    if link:
                        driver.switch_to.window(main_handle)
                        return link
                except Exception:
                    pass
        except Exception as ex:
            print(f"[Smailpro/UI] poll: {type(ex).__name__}: {ex}")

        time.sleep(poll_seconds * 0.75)

    try:
        driver.switch_to.window(main_handle)
    except Exception:
        pass
    return None
