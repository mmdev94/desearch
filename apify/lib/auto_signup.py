import argparse
import json
import os
import re
import random
import secrets
import string
import subprocess
import sys
import time
from pathlib import Path

import requests
import undetected_chromedriver as uc
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

_LIB_DIR = Path(__file__).resolve().parent
APIFY_ROOT = _LIB_DIR.parent
MINER_ROOT = APIFY_ROOT.parent
APOLLO_DIR = MINER_ROOT / "apollo"
if str(APOLLO_DIR) not in sys.path:
    sys.path.insert(0, str(APOLLO_DIR))
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from paths_layout import (  # noqa: E402
    DEFAULT_CREATED_ACCOUNTS,
    DEFAULT_EMAIL_DOMAIN_BLACKLIST,
    DEFAULT_PROXIES_FILE,
)

from apify.lib import config as _apify_cfg  # noqa: E402
from apify.lib.apify_scrape_flow import navigate_actor_input  # noqa: E402
from apify.lib.flow_pause import checkpoint, pause_aware_sleep  # noqa: E402
from apify.lib.human_browser import (  # noqa: E402
    human_click_smooth as _hb_click_smooth,
    human_type_text_slow as _hb_type_text_slow,
)
from smailpro_mailbox import (  # noqa: E402
    create_inbox_with_rotation,
    find_apify_verification_link,
)

from db.pg import connect_ctx, load_apify_env, sync_apify_account_id_sequence  # noqa: E402

# ``2captcha/`` lives at repo root (sibling of ``apify/``), not under ``apify/``.
_TWO_CAPTCHA_DIR = MINER_ROOT / "2captcha"
if str(_TWO_CAPTCHA_DIR) not in sys.path:
    sys.path.insert(0, str(_TWO_CAPTCHA_DIR))
from recaptcha_v2_invisible import solve_recaptcha_v2_invisible  # noqa: E402

OUTPUT_FILE = DEFAULT_CREATED_ACCOUNTS
PROXIES_FILE = DEFAULT_PROXIES_FILE
APIFY_SIGNUP_URL = "https://console.apify.com/sign-up"
APIFY_INTEGRATIONS_URL = "https://console.apify.com/settings/integrations"

_HUMAN_BEHAVIOR = bool(getattr(_apify_cfg, "HUMAN_BEHAVIOR", True))


def _rng_pause(min_s: float, max_s: float) -> None:
    """Human-paced delay that respects Space pause (game-style)."""
    if _HUMAN_BEHAVIOR:
        pause_aware_sleep(random.uniform(min_s, max_s))
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


def _mask_email(address: str) -> str:
    try:
        local, domain = address.split("@", 1)
    except ValueError:
        return address
    if len(local) <= 3:
        local_masked = local[0] + "***" if local else "***"
    else:
        local_masked = f"{local[:2]}***{local[-1:]}"
    return f"{local_masked}@{domain}"


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


def random_full_name() -> str:
    first = [
        "Aiden",
        "Mason",
        "Liam",
        "Noah",
        "Ethan",
        "Lucas",
        "Ella",
        "Mia",
        "Olivia",
        "Ava",
        "Nora",
        "Luna",
    ]
    last = [
        "Carter",
        "Brooks",
        "Parker",
        "Fisher",
        "Turner",
        "Walker",
        "Bennett",
        "Reed",
        "Bailey",
        "Hayes",
        "Miller",
        "Cooper",
    ]
    return f"{random.choice(first)} {random.choice(last)}"


def _outlook_local_part_display_name(email: str) -> str:
    """
    Derive a human-shaped display name from the Outlook local-part (no random first/last lists).
    """
    if "@" not in email:
        return ""
    local = email.split("@", 1)[0].strip()
    local = re.sub(r"[^a-zA-Z0-9._-]+", " ", local)
    raw_parts = [p for p in re.split(r"[._-]+", local) if p and not p.isdigit()]
    parts: list[str] = []
    for p in raw_parts:
        if len(p) == 1:
            continue
        if p.isalpha():
            parts.append(p[:1].upper() + p[1:].lower())
        else:
            parts.append(p)
    out = " ".join(parts).strip()
    return out[:72] if out else ""


def _llm_resolve_model_id(base: str) -> str:
    explicit = (
        os.environ.get("LOCAL_LLM_MODEL") or os.environ.get("OPENAI_MODEL") or os.environ.get("LLM_MODEL") or ""
    ).strip()
    if explicit:
        return explicit
    url = f"{base.rstrip('/')}/models"
    r = requests.get(url, timeout=25)
    r.raise_for_status()
    data = r.json()
    for item in data.get("data") or []:
        mid = item.get("id")
        if mid:
            return str(mid)
    raise RuntimeError("No models from GET /v1/models; set LOCAL_LLM_MODEL")


def _full_name_llm_from_email(email: str) -> str | None:
    """
    OpenAI-compatible local server: one plausible real-person display name from the signup email.
    """
    base = os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:8000/v1").rstrip("/")
    try:
        model = _llm_resolve_model_id(base)
    except Exception:
        return None
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    prompt = (
        "You output ONE plausible real-world person display name (First Last, sometimes middle) "
        "for someone who might own this personal email. It must feel natural on a developer SaaS signup, "
        "not corporate, not joke names, not keywords, not the literal email string.\n\n"
        f"Email local-part and domain are only hints: {email}\n\n"
        'Return JSON only: {"full_name":"First Last"}'
    )
    try:
        r = requests.post(
            f"{base}/chat/completions",
            headers=headers,
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.35,
                "max_tokens": 60,
            },
            timeout=90,
        )
        r.raise_for_status()
        raw = r.json()
        text = ((raw.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        text = str(text).strip()
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                fn = obj.get("full_name")
                if isinstance(fn, str) and fn.strip():
                    return re.sub(r"\s+", " ", fn.strip())[:80]
        except json.JSONDecodeError:
            pass
        m = re.search(r'"full_name"\s*:\s*"([^"]+)"', text)
        if m:
            return re.sub(r"\s+", " ", m.group(1).strip())[:80]
    except Exception:
        return None
    return None


def _choose_signup_display_name(email: str) -> str:
    if (os.environ.get("APIFY_SKIP_NAME_LLM") or "").strip().lower() in ("1", "true", "yes"):
        llm = None
    else:
        llm = _full_name_llm_from_email(email)
    if llm and 4 <= len(llm) <= 80 and "@" not in llm:
        print(f"[Apify] Using LLM-derived display name for onboarding ({len(llm)} chars).")
        return llm
    hint = _outlook_local_part_display_name(email)
    if hint and len(hint) >= 3:
        print("[Apify] Using Outlook local-part derived display name for onboarding.")
        return hint
    print("[Apify] Falling back to random full name for onboarding.")
    return random_full_name()


def _navigate_apify_integrations(driver) -> None:
    driver.get(APIFY_INTEGRATIONS_URL)
    try:
        WebDriverWait(driver, 45).until(
            lambda d: "integrations" in (d.current_url or "").lower()
            or "settings" in (d.current_url or "").lower()
        )
    except Exception:
        pass
    _rng_pause(1.0, 2.0)


def _extract_apify_default_api_token(driver) -> str | None:
    """
    Read the default personal API token from Console integrations (DOM or page source).
    """
    try:
        for tb in driver.find_elements(By.CSS_SELECTOR, "[data-test='toggle-visibility-button']"):
            try:
                if tb.is_displayed():
                    human_click_smooth(driver, tb)
                    _rng_pause(0.2, 0.45)
            except Exception:
                continue
        for sel in (
            "[data-test='IntegrationsTokenListItem-token']",
            "[data-test='IntegrationsTokenListItem-token'] pre code",
            "section[data-test='card'] pre code",
        ):
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    t = (el.text or "").strip()
                    if "apify_api_" in t:
                        m = re.search(r"(apify_api_[A-Za-z0-9_-]{16,})", t)
                        if m:
                            return m.group(1).strip()
                except Exception:
                    continue
        html = driver.page_source or ""
        m = re.search(r"(apify_api_[A-Za-z0-9_-]{16,})", html)
        if m:
            return m.group(1).strip()
    except Exception:
        return None
    return None


def _copy_apify_token_via_clipboard_button(driver) -> str | None:
    """Click Copy on the token row; then try DOM read again (clipboard not always readable in WebDriver)."""
    for btn in driver.find_elements(By.CSS_SELECTOR, "button[data-test='copy_to_clipboard']"):
        try:
            if not btn.is_displayed():
                continue
            human_click_smooth(driver, btn)
            _rng_pause(0.4, 0.9)
            return _extract_apify_default_api_token(driver)
        except Exception:
            continue
    return None


def _find_first_clickable(driver, selectors: list[tuple[By, str]], timeout: int = 20):
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


def _clear_and_type(element, text: str) -> None:
    try:
        element.clear()
    except Exception:
        pass
    element.send_keys(Keys.CONTROL, "a")
    element.send_keys(Keys.BACKSPACE)
    human_type_text_slow(element, text)


def _element_meaningfully_visible(driver, el) -> bool:
    """
    Apify can leave tiny/off-screen reCAPTCHA artifacts visible while onboarding is usable.
    Treat only reasonably-sized on-screen elements as blocking captcha UI.
    """
    try:
        if not el.is_displayed():
            return False
    except Exception:
        return False
    try:
        return bool(
            driver.execute_script(
                """
                const el = arguments[0];
                if (!el || !el.getBoundingClientRect) return false;
                const r = el.getBoundingClientRect();
                if (!r || r.width < 4 || r.height < 4) return false;
                const st = window.getComputedStyle(el);
                if (!st) return true;
                if (st.visibility === 'hidden' || st.display === 'none') return false;
                const op = parseFloat(st.opacity || '1');
                if (!isFinite(op) || op < 0.08) return false;
                return true;
                """,
                el,
            )
        )
    except Exception:
        return True


def _captcha_visible(driver) -> bool:
    markers = [
        (By.CSS_SELECTOR, "iframe[src*='recaptcha']"),
        (By.CSS_SELECTOR, "div.grecaptcha-badge"),
        (By.CSS_SELECTOR, "textarea#g-recaptcha-response"),
        (By.XPATH, "//*[contains(translate(., 'RECAPTCHA', 'recaptcha'), 'recaptcha')]"),
        (By.XPATH, "//*[contains(translate(., 'VERIFY YOU ARE HUMAN', 'verify you are human'), 'verify you are human')]"),
    ]
    for by, selector in markers:
        try:
            for el in driver.find_elements(by, selector):
                if _element_meaningfully_visible(driver, el):
                    return True
        except Exception:
            continue
    return False


def _signup_error_email_taken(driver) -> bool:
    """
    Apify signup UI shows a danger Message when the Outlook address is already registered.
    Detect visible copy containing 'already taken' (e.g. 'This email is already taken.').
    """
    try:
        selectors = [
            (By.XPATH, "//div[contains(@class,'description')][contains(.,'already taken')]"),
            (
                By.XPATH,
                "//div[contains(@class,'Message')][contains(@class,'danger')]"
                "//*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'already taken')]",
            ),
            (
                By.XPATH,
                "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'this email is already taken')]",
            ),
        ]
        for by, sel in selectors:
            for el in driver.find_elements(by, sel):
                try:
                    if el.is_displayed():
                        return True
                except Exception:
                    continue
    except Exception:
        pass
    return False


def _signup_error_account_disabled(driver) -> bool:
    """
    Apify sign-in / console shows a danger Message when the account is disabled
    (e.g. after signup redirect).
    """
    try:
        selectors = [
            (
                By.XPATH,
                "//div[contains(@class,'Message')][contains(@class,'danger')]"
                "//*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
                "'your account was disabled')]",
            ),
            (
                By.XPATH,
                "//div[contains(@class,'description')]"
                "[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
                "'account was disabled')]",
            ),
            (
                By.XPATH,
                "//*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
                "'support@apify.com')]"
                "[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
                "'disabled')]",
            ),
        ]
        for by, sel in selectors:
            for el in driver.find_elements(by, sel):
                try:
                    if el.is_displayed():
                        return True
                except Exception:
                    continue
    except Exception:
        pass
    return False


def _apify_post_signup_ui_ready(driver) -> bool:
    """
    After 2Captcha inject + callback, reCAPTCHA iframes/badge can remain in the DOM while
    Apify has already navigated to /store or shown onboarding. In that case we must not
    block on ``_captcha_visible`` or manual solve.
    """
    try:
        u = (driver.current_url or "").lower()
    except Exception:
        u = ""
    if "console.apify.com" in u and "/store" in u:
        return True
    try:
        for el in driver.find_elements(
            By.CSS_SELECTOR, "[data-test='onboarding-user-info-step']"
        ):
            if _element_meaningfully_visible(driver, el):
                return True
    except Exception:
        pass
    return False


def _wait_for_captcha_clear(driver, *, timeout_sec: int = 420) -> bool:
    """
    Wait for captcha to disappear (manual solve in browser) without requiring
    terminal input; supports multiple concurrent Chrome sessions.
    """
    if not _captcha_visible(driver):
        return True
    print("[Apify] Captcha detected on signup.")
    print("[Apify] Trying automated solve via 2Captcha (fallback: manual solve)...")
    # Best-effort auto solve for reCAPTCHA v2 (invisible).
    try:
        # Extract sitekey from recaptcha iframe URL (k=...)
        sitekey = None
        for fr in driver.find_elements(By.CSS_SELECTOR, "iframe[src*='recaptcha/api2/']"):
            try:
                src = fr.get_attribute("src") or ""
            except Exception:
                continue
            m = re.search(r"[?&]k=([^&]+)", src)
            if m:
                sitekey = m.group(1)
                break
        if not sitekey:
            print("[Apify] Could not find reCAPTCHA sitekey (k=...) in iframe src.")
        else:
            print(f"[Apify] reCAPTCHA sitekey found: {sitekey}")
            load_apify_env()
            print("[Apify] Requesting solution from 2Captcha...")
            sol = solve_recaptcha_v2_invisible(sitekey=sitekey, pageurl=APIFY_SIGNUP_URL)
            token = sol.token
            print(f"[Apify] 2Captcha solved. task_id={sol.task_id}")
            print(f"[Apify] 2Captcha token (prefix): {token[:24]}...")

            print("[Apify] Injecting g-recaptcha-response into page...")
            driver.execute_script(
                """
                const token = arguments[0];
                const els = document.querySelectorAll("textarea#g-recaptcha-response, textarea[name='g-recaptcha-response'], input[name='g-recaptcha-response']");
                for (const el of els) {
                  el.value = token;
                  el.setAttribute("value", token);
                  el.style.display = "block";
                  el.dispatchEvent(new Event("input", {bubbles:true}));
                  el.dispatchEvent(new Event("change", {bubbles:true}));
                }
                """,
                token,
            )
            print("[Apify] Attempting to invoke reCAPTCHA callback (___grecaptcha_cfg)...")
            invoked = driver.execute_script(
                """
                const tok = arguments[0];
                function tryInvoke(cb) {
                  try {
                    if (typeof cb === 'function') { cb(tok); return true; }
                    if (typeof cb === 'string' && typeof window[cb] === 'function') { window[cb](tok); return true; }
                  } catch (e) {}
                  return false;
                }
                let invoked = 0;
                try {
                  const cfg = window.___grecaptcha_cfg;
                  const clients = cfg && cfg.clients ? cfg.clients : null;
                  if (clients) {
                    for (const k of Object.keys(clients)) {
                      const c = clients[k];
                      if (!c) continue;
                      const stack = [c];
                      let steps = 0;
                      while (stack.length && steps < 80) {
                        steps++;
                        const cur = stack.pop();
                        if (!cur || typeof cur !== 'object') continue;
                        if ('callback' in cur) {
                          if (tryInvoke(cur.callback)) invoked++;
                        }
                        for (const kk of Object.keys(cur)) {
                          const v = cur[kk];
                          if (v && typeof v === 'object') stack.push(v);
                        }
                      }
                    }
                  }
                } catch (e) {}
                return invoked;
                """,
                token,
            )
            try:
                print(f"[Apify] Callback invocations: {int(invoked) if invoked is not None else 0}")
            except Exception:
                print("[Apify] Callback invocation count: <unknown>")

            # Some flows expect re-submitting the current step.
            try:
                btn = _find_first_clickable(
                    driver,
                    [
                        (By.XPATH, "//button[@type='submit' and normalize-space()='Sign up']"),
                        (By.XPATH, "//button[@type='submit' and normalize-space()='Continue']"),
                        (By.XPATH, "//button[@type='submit' and normalize-space()='Next']"),
                        (By.CSS_SELECTOR, "button[type='submit']"),
                    ],
                    timeout=3,
                )
                if btn:
                    print("[Apify] Clicking submit button after captcha injection...")
                    human_click_smooth(driver, btn)
            except Exception:
                pass

            # Wait a bit for captcha markers to disappear.
            print("[Apify] Waiting for captcha to clear in browser...")
            _rng_pause(1.5, 2.5)
            if not _captcha_visible(driver):
                print("[Apify] Captcha cleared via 2Captcha; resuming signup flow.")
                return True
            if _apify_post_signup_ui_ready(driver):
                try:
                    ninv = int(invoked) if invoked is not None else 0
                except Exception:
                    ninv = 0
                print(
                    "[Apify] Post-signup UI is ready (/store or onboarding) while reCAPTCHA "
                    f"artifacts remain (callback invocations={ninv}); resuming flow."
                )
                return True
            print("[Apify] Captcha still visible after 2Captcha injection/callback.")
    except Exception as ex:
        print(f"[Apify] 2Captcha solve attempt failed: {type(ex).__name__}: {ex}")

    if _apify_post_signup_ui_ready(driver):
        print(
            "[Apify] Post-signup UI already visible; not waiting for captcha DOM to clear."
        )
        return True

    print("[Apify] Waiting for manual solve in browser...")
    end = time.time() + max(10, timeout_sec)
    while time.time() < end:
        checkpoint()
        if not _captcha_visible(driver):
            print("[Apify] Captcha cleared; resuming signup flow.")
            return True
        if _apify_post_signup_ui_ready(driver):
            print(
                "[Apify] Post-signup UI visible during captcha wait; resuming flow "
                "(lingering reCAPTCHA UI ignored)."
            )
            return True
        _rng_pause(0.6, 1.2)
    print("[Apify] Captcha did not clear in time.")
    return False


def _fill_signup_email_step(driver, email: str) -> bool:
    try:
        driver.get(APIFY_SIGNUP_URL)
    except TimeoutException:
        print("[Apify] Signup page timeout; continuing because page may still be usable.")
    _rng_pause(0.5, 1.0)

    email_input = WebDriverWait(driver, 30).until(
        EC.visibility_of_element_located((By.CSS_SELECTOR, "input[data-test='email'], #email"))
    )
    human_click_smooth(driver, email_input)
    _clear_and_type(email_input, email)
    _rng_pause(0.15, 0.35)

    next_btn = _find_first_clickable(
        driver,
        [
            (By.XPATH, "//button[@type='submit' and normalize-space()='Next']"),
            (By.XPATH, "//button[normalize-space()='Next']"),
        ],
        timeout=12,
    )
    if not next_btn:
        print("[Apify] Could not find enabled Next button.")
        return False
    human_click_smooth(driver, next_btn)
    return True


def _fill_signup_password_step(driver, password: str) -> bool:
    try:
        password_input = WebDriverWait(driver, 20).until(
            EC.visibility_of_element_located(
                (By.CSS_SELECTOR, "input[data-test='password'], input#password")
            )
        )
    except TimeoutException:
        print("[Apify] Password step did not appear after Next.")
        return False

    human_click_smooth(driver, password_input)
    _clear_and_type(password_input, password)
    _rng_pause(0.2, 0.45)

    sign_up_button = _find_first_clickable(
        driver,
        [
            (By.CSS_SELECTOR, "button[data-test='submit-button']"),
            (By.XPATH, "//button[@type='submit' and normalize-space()='Sign up']"),
            (By.XPATH, "//button[normalize-space()='Sign up']"),
        ],
        timeout=15,
    )
    if not sign_up_button:
        print("[Apify] Could not find Sign up button.")
        return False
    human_click_smooth(driver, sign_up_button)
    return True


def _complete_onboarding_name_step(driver, email: str) -> tuple[bool, str]:
    full_name = _choose_signup_display_name(email)

    def _pick_name_input():
        selectors = [
            "div[data-test='onboarding-user-info-step'] input#name",
            "div[data-test='onboarding-user-info-step'] input[name='name']",
            "input#name",
            "input[name='name']",
        ]
        for sel in selectors:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    if _element_meaningfully_visible(driver, el):
                        return el
                except Exception:
                    continue
        return None

    # Prefer finding the onboarding field; captcha widgets can linger on /store without blocking UI.
    name_input = None
    last_captcha_attempt = 0.0
    end_name = time.time() + 180
    while time.time() < end_name:
        if _signup_error_email_taken(driver):
            return False, full_name

        name_input = _pick_name_input()
        if name_input:
            break

        if _captcha_visible(driver) and (time.time() - last_captcha_attempt) > 12:
            last_captcha_attempt = time.time()
            if not _wait_for_captcha_clear(driver):
                print("[Apify] Captcha did not clear while waiting for onboarding name field.")
            if _signup_error_email_taken(driver):
                return False, full_name

        _rng_pause(0.35, 0.8)

    if not name_input:
        if _signup_error_email_taken(driver):
            return False, full_name
        print("[Apify] Onboarding full name input did not appear in time.")
        return False, full_name

    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", name_input)
    except Exception:
        pass
    human_click_smooth(driver, name_input)
    _clear_and_type(name_input, full_name)
    _rng_pause(0.2, 0.45)

    continue_btn = None
    end_btn = time.time() + 45
    while time.time() < end_btn:
        if _signup_error_email_taken(driver):
            return False, full_name
        continue_btn = _find_first_clickable(
            driver,
            [
                (By.CSS_SELECTOR, "div[data-test='onboarding-user-info-step'] button[data-test='submit-button']"),
                (By.CSS_SELECTOR, "button[data-test='submit-button']"),
                (By.XPATH, "//div[@data-test='onboarding-user-info-step']//button[@type='submit' and normalize-space()='Continue']"),
                (By.XPATH, "//button[@type='submit' and normalize-space()='Continue']"),
                (By.XPATH, "//button[normalize-space()='Continue']"),
            ],
            timeout=2,
        )
        if continue_btn:
            break
        # Re-type if the field remounted (SPA) or validation cleared value.
        name_input = _pick_name_input()
        if name_input:
            try:
                cur = (name_input.get_attribute("value") or "").strip()
            except Exception:
                cur = ""
            if cur != full_name.strip():
                try:
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", name_input
                    )
                except Exception:
                    pass
                human_click_smooth(driver, name_input)
                _clear_and_type(name_input, full_name)
        _rng_pause(0.35, 0.7)

    if not continue_btn:
        print("[Apify] Continue button did not become enabled on onboarding step.")
        return False, full_name

    human_click_smooth(driver, continue_btn)
    return True, full_name


def append_created_account(
    path: Path,
    email: str,
    apify_password: str,
    full_name: str,
    mailbox_password: str,
    mailbox_token: str,
) -> None:
    line = f"{email}:{apify_password}:{full_name}:{mailbox_password}:{mailbox_token}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line)


def _email_domain_lower(email: str) -> str:
    if "@" not in email:
        return ""
    return email.rsplit("@", 1)[-1].strip().lower()


def load_email_domain_blacklist(path: Path) -> set[str]:
    """Load domains from file (normalized lowercase); missing file => empty set."""
    if not path.is_file():
        return set()
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        n = line.lower().strip(".")
        if n:
            out.add(n)
    return out


def _apify_email_exists_in_db(email: str) -> bool:
    """
    True if this email is already persisted in Postgres `apify_account`.
    If DB is not configured / table missing, we treat as not existing (best-effort).
    """
    if not email:
        return False
    try:
        load_apify_env()
        with connect_ctx() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM apify_account WHERE lower(trim(email)) = lower(trim(%s)) LIMIT 1",
                    (email,),
                )
                return cur.fetchone() is not None
    except Exception:
        return False


def _persist_apify_no_password_placeholder(email: str, *, tag: str) -> None:
    """
    Record an email as unusable for Apify login (NULL password).

    ``tag`` is only for log lines (e.g. ``email taken``, ``account disabled``).
    Lets ``_apify_email_exists_in_db`` skip it on later runs.
    """
    e = (email or "").strip()
    if not e:
        print(f"[Apify] apify_account ({tag}): skip persist — empty email.")
        return
    masked = _mask_email(e)
    print(f"[Apify] apify_account ({tag}): persisting placeholder for {masked} …")
    try:
        load_apify_env()
        with connect_ctx() as conn:
            sync_apify_account_id_sequence(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, password FROM apify_account
                    WHERE lower(trim(email)) = lower(trim(%s))
                    LIMIT 1
                    """,
                    (e,),
                )
                row = cur.fetchone()
                if row is not None:
                    row_id, pwd = row[0], row[1]
                    if pwd is not None and str(pwd).strip():
                        print(
                            f"[Apify] apify_account ({tag}): {masked} already stored "
                            f"as full account (id={row_id}); no placeholder insert."
                        )
                    else:
                        print(
                            f"[Apify] apify_account ({tag}): {masked} already placeholder "
                            f"(id={row_id}); skip insert."
                        )
                    return
                cur.execute(
                    """
                    INSERT INTO apify_account (email, password, full_name, mailbox_password, mailbox_token)
                    VALUES (%s, NULL, NULL, NULL, NULL)
                    """,
                    (e,),
                )
            conn.commit()
        print(
            f"[Apify] apify_account ({tag}): INSERT OK — NULL password placeholder for {masked} "
            "(will be skipped on future inbox precheck)."
        )
    except Exception as ex:
        print(
            f"[Apify] apify_account ({tag}): FAILED placeholder for {masked} — "
            f"{type(ex).__name__}: {ex}"
        )


def _persist_apify_external_email_placeholder(email: str) -> None:
    """
    Record an Outlook address Apify reports as **already registered** (no password).
    """
    _persist_apify_no_password_placeholder(email, tag="email taken")


def _persist_apify_account_to_db(
    *,
    email: str,
    apify_password: str,
    full_name: str,
    mailbox_password: str,
    mailbox_token: str,
    api_token: str | None = None,
    api_status: str | None = None,
) -> None:
    masked = _mask_email((email or "").strip())
    print(f"[Apify] apify_account: saving full credentials to Postgres for {masked} …")
    api_status_val = (api_status or "Not used yet").strip() or "Not used yet"
    try:
        load_apify_env()
        with connect_ctx() as conn:
            sync_apify_account_id_sequence(conn)
            with conn.cursor() as cur:
                if api_token is not None and str(api_token).strip():
                    tok = str(api_token).strip()
                    cur.execute(
                        """
                        UPDATE apify_account
                        SET password = %s,
                            full_name = %s,
                            mailbox_password = %s,
                            mailbox_token = %s,
                            api_token = %s,
                            api_status = %s,
                            api_run_count = 0
                        WHERE lower(trim(email)) = lower(trim(%s))
                        """,
                        (
                            apify_password,
                            full_name,
                            mailbox_password,
                            mailbox_token,
                            tok,
                            api_status_val,
                            email,
                        ),
                    )
                    updated = cur.rowcount
                    if updated == 0:
                        cur.execute(
                            """
                            INSERT INTO apify_account (
                                email, password, full_name, mailbox_password, mailbox_token,
                                api_token, api_status, api_run_count
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, 0)
                            """,
                            (
                                email,
                                apify_password,
                                full_name,
                                mailbox_password,
                                mailbox_token,
                                tok,
                                api_status_val,
                            ),
                        )
                        action = "INSERT"
                    else:
                        action = "UPDATE"
                else:
                    cur.execute(
                        """
                        UPDATE apify_account
                        SET password = %s,
                            full_name = %s,
                            mailbox_password = %s,
                            mailbox_token = %s
                        WHERE lower(trim(email)) = lower(trim(%s))
                        """,
                        (apify_password, full_name, mailbox_password, mailbox_token, email),
                    )
                    updated = cur.rowcount
                    if updated == 0:
                        cur.execute(
                            """
                            INSERT INTO apify_account (email, password, full_name, mailbox_password, mailbox_token)
                            VALUES (%s, %s, %s, %s, %s)
                            """,
                            (email, apify_password, full_name, mailbox_password, mailbox_token),
                        )
                        action = "INSERT"
                    else:
                        action = "UPDATE"
            conn.commit()
        if action == "UPDATE":
            print(
                f"[Apify] apify_account: Postgres UPDATE OK for {masked} ({updated} row(s))."
            )
        else:
            print(f"[Apify] apify_account: Postgres INSERT OK for {masked}.")
    except Exception as ex:
        print(
            f"[Apify] apify_account: FAILED to save credentials to Postgres for {masked} — "
            f"{type(ex).__name__}: {ex}"
        )


def _domain_matches_blacklist(domain: str, blocked: set[str]) -> bool:
    """
    True if domain is blocked: exact match, or (for multi-label rules) any subdomain of it.

    List the registrable / final hostname only, e.g. ``xn--yaho-sqa.com`` — then
    ``edu.xn--yaho-sqa.com``, ``best.xn--yaho-sqa.com`` match automatically.
    Single-label rules (no dot) match that label only (avoids blocking all of ``.com``).
    """
    d = domain.strip().lower().strip(".")
    if not d:
        return False
    for raw in blocked:
        b = raw.strip().lower().strip(".")
        if not b:
            continue
        if d == b:
            return True
        if "." in b and d.endswith("." + b):
            return True
    return False


def create_single_apify_account(
    driver,
    *,
    append_to: Path | None = None,
    post_signup: str = "actor",
) -> dict[str, str] | None:
    """
    One full signup + verify, then either actor input or integrations API token page.

    ``post_signup``:
    - ``"actor"`` (default): open the Apify actor JSON input page (legacy miner flow).
    - ``"integrations_token"``: open ``/settings/integrations``, read default personal API token,
      persist ``api_token`` + ``api_status`` (``Not used yet``) to Postgres.

    If Apify shows **This email is already taken** (e.g. after captcha), the address is stored
    in Postgres ``apify_account`` with **NULL password** (precheck only), then the same browser
    session requests another Outlook via Smailpro using ``reuse_mail_session`` (no second Sonjj
    login when the mail tab is already open). Retries until success or ``APIFY_SIGNUP_EMAIL_TAKEN_MAX_ROUNDS``.

    Keys: email, apify_password, full_name, mailbox_password (always empty here),
    mailbox_token (Smailpro temp Outlook address — same as email for UI flow);
    optional ``api_token`` when ``post_signup="integrations_token"``.
    """
    mailbox_password = ""
    blacklist_path = getattr(
        _apify_cfg, "EMAIL_DOMAIN_BLACKLIST_FILE", DEFAULT_EMAIL_DOMAIN_BLACKLIST
    )
    if not isinstance(blacklist_path, Path):
        blacklist_path = Path(blacklist_path)
    max_inbox = int(
        getattr(_apify_cfg, "EMAIL_DOMAIN_BLACKLIST_MAX_INBOX_ATTEMPTS", 40)
    )
    max_inbox = max(1, max_inbox)
    blocked_domains = load_email_domain_blacklist(blacklist_path.resolve())
    max_taken_rounds = int(
        getattr(_apify_cfg, "APIFY_SIGNUP_EMAIL_TAKEN_MAX_ROUNDS", 30)
    )
    max_taken_rounds = max(1, max_taken_rounds)
    smailpro_mail_ready = False

    for taken_round in range(1, max_taken_rounds + 1):
        checkpoint()
        if taken_round > 1:
            print(
                f"[Apify] Retrying signup ({taken_round}/{max_taken_rounds}): "
                "new Outlook inbox, same browser (previous address already on Apify)..."
            )

        email = ""
        email_id = ""
        for attempt in range(1, max_inbox + 1):
            checkpoint()
            try:
                email, email_id, _ = create_inbox_with_rotation(
                    driver=driver,
                    apify_root=APIFY_ROOT,
                    usage_file=None,
                    reuse_mail_session=smailpro_mail_ready,
                )
            except Exception as exc:
                print(f"[Apify] Smailpro temp Outlook creation failed: {type(exc).__name__}: {exc}")
                return None
            smailpro_mail_ready = True

            dom = _email_domain_lower(email)
            if dom and _domain_matches_blacklist(dom, blocked_domains):
                print(
                    f"[Apify] Blacklisted domain {dom!r} (attempt {attempt}/{max_inbox}); "
                    "requesting another inbox..."
                )
                continue
            if _apify_email_exists_in_db(email):
                print(
                    f"[Apify] Email already in apify_account table (attempt {attempt}/{max_inbox}); "
                    "requesting another inbox..."
                )
                continue
            if attempt > 1:
                print(f"[Apify] Using non-blacklisted inbox after {attempt} attempt(s).")
            break
        else:
            print(
                f"[Apify] Could not obtain inbox off blacklist after {max_inbox} attempt(s). "
                f"Edit {blacklist_path.name} or raise EMAIL_DOMAIN_BLACKLIST_MAX_INBOX_ATTEMPTS."
            )
            return None

        mailbox_token = email_id
        print(f"[Apify] Starting signup for: {_mask_email(email)}")
        apify_password = random_strong_password()
        ok_email = _fill_signup_email_step(driver, email)
        if not ok_email:
            print("[Apify] Failed at email step.")
            return None

        _rng_pause(0.35, 0.9)
        if _signup_error_email_taken(driver):
            _persist_apify_external_email_placeholder(email)
            print(
                "[Apify] Email already registered on Apify; creating another Outlook inbox "
                "(same browser)..."
            )
            continue

        ok_password = _fill_signup_password_step(driver, apify_password)
        if not ok_password:
            if _signup_error_email_taken(driver):
                _persist_apify_external_email_placeholder(email)
                print(
                    "[Apify] Email already taken at password step; new Outlook inbox "
                    "(same browser)..."
                )
                continue
            print("[Apify] Failed at password/signup step.")
            return None

        _rng_pause(0.35, 0.9)
        if _signup_error_email_taken(driver):
            _persist_apify_external_email_placeholder(email)
            print(
                "[Apify] Email already taken after password; new Outlook inbox (same browser)..."
            )
            continue

        ok_onboard, full_name = _complete_onboarding_name_step(driver, email)
        if not ok_onboard:
            if _signup_error_email_taken(driver):
                _persist_apify_external_email_placeholder(email)
                print(
                    "[Apify] Email already taken (e.g. after captcha); new Outlook inbox "
                    "(same browser)..."
                )
                continue
            print("[Apify] Failed at onboarding name step.")
            return None

        if _signup_error_email_taken(driver):
            _persist_apify_external_email_placeholder(email)
            print(
                "[Apify] Email already taken after onboarding; new Outlook inbox (same browser)..."
            )
            continue

        print(
            "[Apify] Waiting for verification email (subject contains "
            "'verify your email address for apify')..."
        )
        verify_link = find_apify_verification_link(driver, email)
        if not verify_link:
            print("[Apify] Verification email not found in time.")
            return None

        print("[Apify] Verification link found. Waiting before opening (new tab)...")
        original_handle = driver.current_window_handle
        _rng_pause(2.0, 4.0)
        driver.execute_script("window.open('about:blank','_blank');")
        handles = driver.window_handles
        new_handle = next((h for h in handles if h != original_handle), None)
        if not new_handle:
            print("[Apify] Could not open new tab; opening verify link in current tab.")
            try:
                driver.get(verify_link)
            except TimeoutException:
                print("[Apify] Verify link navigation timeout; continuing.")
            _rng_pause(3.0, 4.2)
        else:
            driver.switch_to.window(new_handle)
            try:
                driver.get(verify_link)
            except TimeoutException:
                print("[Apify] Verify link navigation timeout; continuing.")
            _rng_pause(3.0, 4.2)
            try:
                driver.switch_to.window(original_handle)
                driver.close()
            except Exception:
                pass
            remaining = driver.window_handles
            if remaining:
                driver.switch_to.window(remaining[0])

        _rng_pause(0.8, 1.5)
        if _signup_error_account_disabled(driver):
            _persist_apify_no_password_placeholder(email, tag="account disabled")
            print(
                "[Apify] Account disabled on Apify (post-verification); "
                "retrying full signup with a new Outlook inbox (same browser)..."
            )
            continue

        api_token: str | None = None
        if post_signup == "integrations_token":
            print("[Apify] Opening Apify integrations (personal API token)…")
            _navigate_apify_integrations(driver)
            _rng_pause(1.0, 2.0)
            if _signup_error_account_disabled(driver):
                _persist_apify_no_password_placeholder(email, tag="account disabled")
                print(
                    "[Apify] Account disabled on Apify console after signup; "
                    "retrying full signup with a new Outlook inbox (same browser)..."
                )
                continue
            api_token = _extract_apify_default_api_token(driver)
            if not api_token:
                api_token = _copy_apify_token_via_clipboard_button(driver)
            if not api_token:
                print("[Apify] Could not read default API token from integrations page.")
                return None
        else:
            print("[Apify] Redirecting to actor input page...")
            navigate_actor_input(driver)
            _rng_pause(1.0, 2.0)
            if _signup_error_account_disabled(driver):
                _persist_apify_no_password_placeholder(email, tag="account disabled")
                print(
                    "[Apify] Account disabled on Apify console after signup; "
                    "retrying full signup with a new Outlook inbox (same browser)..."
                )
                continue

        out = {
            "email": email,
            "apify_password": apify_password,
            "full_name": full_name,
            "mailbox_password": mailbox_password,
            "mailbox_token": mailbox_token,
        }
        if api_token:
            out["api_token"] = api_token
        _persist_apify_account_to_db(
            email=email,
            apify_password=apify_password,
            full_name=full_name,
            mailbox_password=mailbox_password,
            mailbox_token=mailbox_token,
            api_token=api_token if post_signup == "integrations_token" else None,
            api_status="Not used yet" if post_signup == "integrations_token" else None,
        )
        if append_to is not None:
            append_created_account(
                append_to,
                email,
                apify_password,
                full_name,
                mailbox_password,
                mailbox_token,
            )
            print(f"[Apify] Saved credentials to {append_to.name}")
        return out

    print(
        f"[Apify] Stopped after {max_taken_rounds} attempt(s): "
        "Apify kept rejecting signup (e.g. email already taken or account disabled)."
    )
    return None


def run_apify_signup_flow(count: int) -> None:
    # Reuse the shared UC builder so proxy config matches other Apify flows.
    from apify.lib import config  # noqa: E402
    from apify.lib.apify_scrape_flow import build_uc_driver  # noqa: E402
    from proxy_pool import pick_proxy  # noqa: E402

    px = pick_proxy(PROXIES_FILE) if config.PROXY else None
    if px:
        print(f"[Browser] Using proxy: {px.url}")
    driver = build_uc_driver(proxy_server=px.url if px else None)
    try:
        for i in range(1, count + 1):
            print(f"\n[{i}/{count}] Creating Apify account...")
            created = create_single_apify_account(
                driver,
                append_to=OUTPUT_FILE,
            )
            if not created:
                print(f"[{i}/{count}] Signup flow failed.")
                continue
            print(
                f"[{i}/{count}] Signup finished. "
                f"Saved credentials to {OUTPUT_FILE.name}"
            )
            _rng_pause(1.0, 2.0)
    finally:
        driver.quit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Apify auto-signup via Smailpro UI (Sonjj login) temp Outlook + full name step"
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        help="How many accounts to create in this run",
    )
    args = parser.parse_args()

    run_apify_signup_flow(count=max(1, args.count))
