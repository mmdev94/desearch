from __future__ import annotations

import itertools
import random
import re
import secrets
import shutil
import string
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from apify.lib.flow_pause import checkpoint, pause_aware_sleep
from apify.lib.human_browser import human_click_smooth, human_type_text_slow
from apify.lib.smailpro_mailbox import create_inbox_with_rotation, find_twex_verification_link
from db.pg import connect_ctx, load_env
from twex.lib.uc_wire_driver import build_twex_wire_driver

_LIB_DIR = Path(__file__).resolve().parent
_TWEX_ROOT = _LIB_DIR.parent
_REPO_ROOT = _TWEX_ROOT.parent
APIFY_ROOT = _REPO_ROOT / "apify"
DEFAULT_PROXIES_FILE = _TWEX_ROOT / "proxies.txt"
MAX_INBOX_ATTEMPTS = 40


def _proxy_line_to_chrome_proxy_server(line: str) -> str | None:
    """
    Map one non-empty line to Chrome ``--proxy-server=`` value (HTTP).

    Supports:
    - ``http(s)://host:port`` or ``http(s)://user:pass@host:port``
    - ``host:port:user:password`` (common provider format)
    - ``host:port``
    """
    raw = (line or "").strip()
    if not raw or raw.startswith("#"):
        return None
    low = raw.lower()
    if low.startswith("http://") or low.startswith("https://"):
        return raw

    parts = raw.rsplit(":", 3)
    if len(parts) == 4 and parts[1].isdigit():
        host, port_s, user, password = parts
        u = quote(user, safe="")
        p = quote(password, safe="")
        return f"http://{u}:{p}@{host}:{port_s}"

    if ":" in raw:
        host_part, port_part = raw.rsplit(":", 1)
        if port_part.isdigit():
            return f"http://{host_part}:{port_part}"

    return None


def _proxy_log_label(proxy_server: str) -> str:
    """Mask credentials for log lines."""
    s = proxy_server.strip()
    if "@" in s and "://" in s:
        try:
            scheme, rest = s.split("://", 1)
            auth, hostport = rest.rsplit("@", 1)
            if ":" in auth:
                user, _ = auth.split(":", 1)
                return f"{scheme}://{user}:***@{hostport}"
        except ValueError:
            pass
    return s


def load_http_proxy_servers(path: Path) -> list[str]:
    if not path.is_file():
        raise FileNotFoundError(f"Proxies file not found: {path}")
    out: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        p = _proxy_line_to_chrome_proxy_server(line)
        if p:
            out.append(p)
    return out

TWEX_LOGIN_URL = "https://twexapi.io/auth/simple-login"
TWEX_DASHBOARD_URL = "https://twexapi.io/dashboard"


def _rng_pause(min_s: float, max_s: float) -> None:
    pause_aware_sleep(random.uniform(min_s, max_s))


def random_strong_password(length: int = 18) -> str:
    if length < 14:
        length = 14
    lower = secrets.choice(string.ascii_lowercase)
    upper = secrets.choice(string.ascii_uppercase)
    digit = secrets.choice(string.digits)
    special = secrets.choice("!@#$%^&*()-_=+[]{}")
    pool = string.ascii_letters + string.digits + "!@#$%^&*()-_=+[]{}"
    rest = [secrets.choice(pool) for _ in range(length - 4)]
    chars = [lower, upper, digit, special, *rest]
    random.SystemRandom().shuffle(chars)
    return "".join(chars)


def _twex_email_exists_in_db(email: str) -> bool:
    if not email:
        return False
    try:
        load_env()
        with connect_ctx() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM public.twex_account WHERE lower(trim(email)) = lower(trim(%s)) LIMIT 1",
                    (email,),
                )
                return cur.fetchone() is not None
    except Exception:
        return False


def _find_clickable_by_text(driver, *, texts: list[str], timeout: int = 30):
    end = time.time() + timeout
    while time.time() < end:
        checkpoint()

        def _xpath_literal(value: str) -> str:
            # XPath string escape helper; supports values containing apostrophes.
            if "'" not in value:
                return f"'{value}'"
            if '"' not in value:
                return f'"{value}"'
            parts = value.split("'")
            concat_parts: list[str] = []
            for idx, part in enumerate(parts):
                if part:
                    concat_parts.append(f"'{part}'")
                if idx < len(parts) - 1:
                    concat_parts.append('"\'"')
            return "concat(" + ", ".join(concat_parts) + ")"

        xpath = " | ".join(
            "//button[contains("
            "translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
            f"{_xpath_literal(t.lower())}"
            ")]"
            for t in texts
        )
        els = driver.find_elements(By.XPATH, xpath)
        for el in els:
            try:
                if el.is_displayed() and el.is_enabled():
                    return el
            except Exception:
                continue
        _rng_pause(0.15, 0.4)
    return None


def _clear_and_type(element, text: str) -> None:
    try:
        element.clear()
    except Exception:
        pass
    element.send_keys(Keys.CONTROL, "a")
    element.send_keys(Keys.BACKSPACE)
    human_type_text_slow(element, text)


def _fill_signup_form(driver, *, email: str, password: str) -> None:
    driver.get(TWEX_LOGIN_URL)
    WebDriverWait(driver, 30).until(
        EC.presence_of_element_located(
            (By.XPATH, "//*[contains(translate(.,'SIGN IN TO TWEXAPI','sign in to twexapi'),'sign in to twexapi')]")
        )
    )
    _rng_pause(0.4, 0.8)
    email_btn = _find_clickable_by_text(driver, texts=["sign in with email"], timeout=20)
    if not email_btn:
        raise RuntimeError("Could not find 'Sign in with email' button.")
    human_click_smooth(driver, email_btn)
    _rng_pause(0.4, 0.8)
    switch_btn = _find_clickable_by_text(driver, texts=["don't have an account? sign up"], timeout=25)
    if not switch_btn:
        raise RuntimeError("Could not switch to sign-up form.")
    human_click_smooth(driver, switch_btn)
    _rng_pause(0.4, 0.8)
    email_input = WebDriverWait(driver, 30).until(
        EC.visibility_of_element_located((By.CSS_SELECTOR, "#simple-login-email"))
    )
    pass_input = WebDriverWait(driver, 30).until(
        EC.visibility_of_element_located((By.CSS_SELECTOR, "#simple-login-password"))
    )
    human_click_smooth(driver, email_input)
    _clear_and_type(email_input, email)
    _rng_pause(0.1, 0.3)
    human_click_smooth(driver, pass_input)
    _clear_and_type(pass_input, password)
    _rng_pause(0.2, 0.45)
    submit_btn = _find_clickable_by_text(
        driver,
        texts=["create account", "sign up", "sign in with email"],
        timeout=20,
    )
    if not submit_btn:
        raise RuntimeError("Could not find submit button on Twex signup form.")
    human_click_smooth(driver, submit_btn)


def _extract_dashboard_api_key(driver) -> str | None:
    def _clean_unmasked_key(text: str) -> str | None:
        t = (text or "").strip()
        if not t:
            return None
        # Masked/truncated renders are invalid for DB save.
        if "*" in t or "..." in t:
            return None
        m = re.search(r"(twitterx_[A-Za-z0-9]{40,96})", t)
        return m.group(1) if m else None

    def _scan_key_cards() -> tuple[str | None, bool]:
        """
        Returns (unmasked_key, masked_seen).
        masked_seen=True means at least one twitterx_* code was present but masked.
        """
        masked_seen = False
        cards = driver.find_elements(
            By.XPATH,
            "//div[contains(@class,'rounded') and .//code[contains(.,'twitterx_')]]",
        )
        for card in cards:
            try:
                for node in card.find_elements(By.CSS_SELECTOR, "code"):
                    text = (node.text or "").strip()
                    if "twitterx_" not in text:
                        continue
                    if "*" in text or "..." in text:
                        masked_seen = True
                        continue
                    key = _clean_unmasked_key(text)
                    if key:
                        return key, masked_seen
            except Exception:
                continue

        # Fallback: global code scan (some layouts do not keep strict card structure).
        for node in driver.find_elements(By.CSS_SELECTOR, "code"):
            try:
                text = (node.text or "").strip()
                if "twitterx_" not in text:
                    continue
                if "*" in text or "..." in text:
                    masked_seen = True
                    continue
                key = _clean_unmasked_key(text)
                if key:
                    return key, masked_seen
            except Exception:
                continue
        return None, masked_seen

    def _click_eye_or_copy_on_key_card() -> bool:
        """
        Click eye/copy controls near twitterx key card.
        We use both because some dashboards unmask after eye toggle; others refresh/render after copy.
        """
        clicked = False
        cards = driver.find_elements(
            By.XPATH,
            "//div[contains(@class,'rounded') and .//code[contains(.,'twitterx_')]]",
        )
        for card in cards:
            try:
                for btn in card.find_elements(By.XPATH, ".//button[.//*[name()='svg']]"):
                    if not btn.is_displayed() or not btn.is_enabled():
                        continue
                    svg_cls = ""
                    try:
                        svg = btn.find_element(By.XPATH, ".//*[name()='svg']")
                        svg_cls = (svg.get_attribute("class") or "").lower()
                    except Exception:
                        pass
                    if "eye" in svg_cls or "copy" in svg_cls:
                        human_click_smooth(driver, btn)
                        clicked = True
                        _rng_pause(0.12, 0.28)
            except Exception:
                continue
        return clicked

    driver.get(TWEX_DASHBOARD_URL)
    WebDriverWait(driver, 40).until(
        lambda d: "dashboard" in (d.current_url or "").lower()
    )
    _rng_pause(1.2, 2.0)
    for _ in range(8):
        checkpoint()
        key, masked_seen = _scan_key_cards()
        if key:
            return key

        # If we explicitly see masked twitterx value, force eye/copy retry.
        if masked_seen and _click_eye_or_copy_on_key_card():
            _rng_pause(0.35, 0.7)
            key, _ = _scan_key_cards()
            if key:
                return key

        eye_btn = _find_clickable_by_text(driver, texts=["api key"], timeout=2)
        if eye_btn:
            try:
                human_click_smooth(driver, eye_btn)
            except Exception:
                pass
        if _click_eye_or_copy_on_key_card():
            _rng_pause(0.3, 0.6)

        # Sometimes the page updates key text without changing layout.
        key, _ = _scan_key_cards()
        if key:
            return key
        _rng_pause(0.6, 1.1)

    # Final read from visible code blocks only.
    key, _ = _scan_key_cards()
    if key:
        return key

    html = driver.page_source or ""
    m = re.search(r"(twitterx_[A-Za-z0-9]{40,96})", html)
    return m.group(1) if m else None


def _save_twex_account(email: str, password: str, api_key: str | None) -> None:
    load_env()
    credit = 20000.0 if email and password and (api_key or "").strip() else 0.0
    with connect_ctx() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE public.twex_account
                SET password = %s,
                    api_key = %s,
                    credit_amount = %s
                WHERE lower(trim(email)) = lower(trim(%s))
                """,
                (password, api_key, credit, email),
            )
            if cur.rowcount == 0:
                cur.execute(
                    """
                    INSERT INTO public.twex_account (email, password, api_key, credit_amount)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (email, password, api_key, credit),
                )
        conn.commit()


def create_single_twex_account(driver) -> dict[str, str] | None:
    smailpro_mail_ready = False
    email = ""
    for attempt in range(1, MAX_INBOX_ATTEMPTS + 1):
        email, _, _ = create_inbox_with_rotation(
            driver=driver,
            apify_root=APIFY_ROOT,
            usage_file=None,
            reuse_mail_session=smailpro_mail_ready,
            provider="gmail",
        )
        smailpro_mail_ready = True
        if _twex_email_exists_in_db(email):
            print(
                f"[Twex] Email already in twex_account; regenerating Gmail "
                f"({attempt}/{MAX_INBOX_ATTEMPTS})…"
            )
            continue
        break
    else:
        raise RuntimeError(
            f"Could not obtain a Gmail address not already in twex_account after "
            f"{MAX_INBOX_ATTEMPTS} attempts."
        )

    twex_password = random_strong_password()
    print(f"[Twex] Smailpro Gmail inbox: {email}")
    _fill_signup_form(driver, email=email, password=twex_password)
    verify_link = find_twex_verification_link(driver, email, timeout_seconds=300)
    if not verify_link:
        raise RuntimeError("Twex verification email not found in Smailpro inbox.")
    print("[Twex] Verification link opened (new tab from Smailpro); continuing on Twex session.")
    _rng_pause(1.0, 1.8)
    api_key = _extract_dashboard_api_key(driver)
    _save_twex_account(email, twex_password, api_key)
    if not api_key:
        print("[Twex] Warning: API key not extracted; saved account with credit 0.")
    else:
        print(f"[Twex] Saved account and API key (len={len(api_key)}).")
    return {
        "email": email,
        "password": twex_password,
        "api_key": api_key or "",
        "mailbox_password": "",
    }


def run_twex_signup_flow(
    *,
    count: int | None = None,
    proxies_file: Path | None = None,
) -> None:
    """
    Create Twex accounts in a loop using HTTP proxies from ``proxies.txt``.

    - ``count`` ``None``: run until Ctrl+C (each iteration = one browser session + one signup attempt).
    - ``count`` positive: stop after that many iterations.

    Proxies rotate round-robin (first → last → first → …). One proxy per iteration; browser is quit after each attempt.

    Each attempt uses a **fresh temporary** ``user-data-dir`` so the driver does not add
    ``--incognito`` (avoids undetected-chrome + HTTP proxy issues with incognito).

    Proxies are applied via **selenium-wire** (upstream HTTP proxy), not Chrome's ``--proxy-server``.
    """
    path = proxies_file if proxies_file is not None else DEFAULT_PROXIES_FILE
    proxies = load_http_proxy_servers(path)
    if not proxies:
        raise RuntimeError(f"No usable HTTP proxy lines in {path}")
    print(f"[Twex] Loaded {len(proxies)} HTTP proxies from {path.name}; rotating after each attempt.")

    proxy_cycle = itertools.cycle(proxies)
    iteration = 0
    try:
        while True:
            iteration += 1
            if count is not None and iteration > count:
                break

            limit_txt = f"{iteration}/{count}" if count is not None else str(iteration)
            proxy_server = next(proxy_cycle)
            print(
                f"\n[Twex {limit_txt}] Starting account flow "
                f"(proxy {_proxy_log_label(proxy_server)}) …"
            )
            profile_dir = tempfile.mkdtemp(prefix="twex_uc_")
            try:
                driver = build_twex_wire_driver(
                    proxy_server=proxy_server,
                    user_data_dir=profile_dir,
                    headless=False,
                    clear_site_data_before_use=False,
                )
                try:
                    created = create_single_twex_account(driver)
                    if created:
                        print(f"[Twex {limit_txt}] Done for {created['email']}.")
                    else:
                        print(f"[Twex {limit_txt}] Flow returned no account.")
                except KeyboardInterrupt:
                    raise
                except Exception as ex:
                    print(f"[Twex {limit_txt}] Error — {type(ex).__name__}: {ex}")
                finally:
                    try:
                        driver.quit()
                    except Exception:
                        pass
            finally:
                shutil.rmtree(profile_dir, ignore_errors=True)
    except KeyboardInterrupt:
        print("\n[Twex] Stopped (Ctrl+C).")
