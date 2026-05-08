"""
Twex: undetected Chrome + **selenium-wire** upstream HTTP proxy.

Chrome talks to selenium-wire's local mitm proxy; selenium-wire forwards to your ``http://user:pass@host:port``
provider. Do **not** pass ``--proxy-server`` on Chrome for upstream auth — wire handles it.
"""
from __future__ import annotations

import seleniumwire.undetected_chromedriver as uc

from apify.lib.apify_scrape_flow import _detect_local_chrome_major, clear_chrome_browsing_data
from apify.lib.human_browser import reset_synthetic_mouse


def build_twex_wire_driver(
    *,
    proxy_server: str,
    user_data_dir: str | None = None,
    profile_directory: str | None = None,
    headless: bool = False,
    clear_site_data_before_use: bool = False,
) -> uc.Chrome:
    """
    Mirror ``build_uc_driver`` options (fingerprint, temp profile → no incognito) but route traffic
    through selenium-wire's proxy backend instead of Chrome's ``--proxy-server`` for upstream.
    """
    chrome_major_detected = _detect_local_chrome_major()
    major = chrome_major_detected or 131
    fp = None
    try:
        from apify.lib import config as _apify_cfg

        if getattr(_apify_cfg, "BROWSER_FINGERPRINT", True):
            from browser_fingerprint import apply_fingerprint, pick_fingerprint

            fp = pick_fingerprint(chrome_major=major)
    except Exception:
        fp = None

    options = uc.ChromeOptions()
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--allow-insecure-localhost")
    if headless:
        options.add_argument("--headless=new")

    udd = (user_data_dir or "").strip()
    if udd:
        options.add_argument(f"--user-data-dir={udd}")
        pd = (profile_directory or "").strip()
        if pd:
            options.add_argument(f"--profile-directory={pd}")
        print(f"[browser wire] user-data-dir={udd}" + (f" profile-directory={pd}" if pd else ""))

    incognito_enabled = False
    try:
        from apify.lib import config as _apify_cfg2

        incognito_enabled = bool(getattr(_apify_cfg2, "INCOGNITO", False))
    except Exception:
        incognito_enabled = False
    if udd:
        if incognito_enabled:
            print("[browser wire] Skipping incognito (persistent user-data-dir in use)")
    elif incognito_enabled:
        options.add_argument("--incognito")
        print("[browser wire] Incognito mode enabled")

    if fp is not None:
        options.add_argument(f"--lang={fp.locale}")
        options.add_argument(f"--window-size={fp.viewport_width},{fp.viewport_height}")
        options.add_experimental_option(
            "prefs",
            {"intl.accept_languages": fp.accept_languages},
        )
        print(
            f"[browser wire] Profile: {fp.platform_label} | tz={fp.timezone_id} | "
            f"lang={fp.locale} | {fp.viewport_width}x{fp.viewport_height} @ {fp.device_scale_factor}x"
        )

    proxy_url = (proxy_server or "").strip()
    if not proxy_url:
        raise ValueError("proxy_server is required for build_twex_wire_driver")
    seleniumwire_options = {
        "proxy": {
            "http": proxy_url,
            "https": proxy_url,
            "no_proxy": "localhost,127.0.0.1",
        },
    }
    print("[browser wire] selenium-wire upstream HTTP proxy enabled (not Chrome --proxy-server).")

    driver = uc.Chrome(
        options=options,
        seleniumwire_options=seleniumwire_options,
        use_subprocess=True,
        version_main=chrome_major_detected,
    )
    driver.implicitly_wait(2)
    driver.set_page_load_timeout(120)
    if fp is not None:
        try:
            apply_fingerprint(driver, fp)
        except Exception as exc:
            print(f"[browser wire] Fingerprint CDP partial failure (continuing): {exc}")
    reset_synthetic_mouse()
    if clear_site_data_before_use:
        try:
            clear_chrome_browsing_data(driver)
            print("[browser wire] Cleared cookies/cache and Apify site storage (CDP)")
        except Exception as exc:
            print(f"[browser wire] CDP storage clear partial failure (continuing): {exc}")
    return driver
