"""
Realistic rotating browser surface for Apify automation (UC + CDP).

Sets: user agent (aligned with local Chrome major), viewport, device scale,
timezone, accept-languages / locale. All presets are modern (2024–2026 style)
consumer setups — no ancient browsers or obvious bot strings.

Note: True random WebGL/canvas fingerprint changes usually require injected JS,
which many sites flag as tampering. We avoid that; CDP + prefs cover what
Chrome exposes cleanly.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass


@dataclass(frozen=True)
class BrowserFingerprint:
    """One coherent “machine + browser” snapshot."""

    # Full User-Agent (Chrome version should match installed major when possible).
    user_agent: str
    viewport_width: int
    viewport_height: int
    device_scale_factor: float
    timezone_id: str
    accept_languages: str
    locale: str
    platform_label: str  # for logging


def _chrome_build_suffix(major: int) -> str:
    """Plausible patch/build for UA (not verified against real build)."""
    a = secrets.randbelow(120) + 6400
    b = secrets.randbelow(200) + 80
    return f"{major}.0.{a}.{b}"


def _profiles_for_major(major: int) -> list[BrowserFingerprint]:
    v = _chrome_build_suffix(major)
    ua_win = (
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{v} Safari/537.36"
    )
    ua_win11 = (
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{v} Safari/537.36"
    )
    ua_mac = (
        f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{v} Safari/537.36"
    )
    ua_linux = (
        f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{v} Safari/537.36"
    )

    # (ua, w, h, dpr, tz, langs, locale, log_platform)
    raw: list[tuple[str, int, int, float, str, str, str, str]] = [
        (ua_win11, 1920, 969, 1.0, "America/New_York", "en-US,en", "en-US", "Win10 en-US Eastern"),
        (ua_win11, 1920, 969, 1.25, "America/Chicago", "en-US,en", "en-US", "Win10 en-US Central"),
        (ua_win11, 1536, 754, 1.25, "America/Los_Angeles", "en-US,en", "en-US", "Win10 en-US Pacific"),
        (ua_win11, 1680, 939, 1.0, "America/Denver", "en-US,en", "en-US", "Win10 en-US Mountain"),
        (ua_win11, 1920, 969, 1.5, "America/Toronto", "en-CA,en-US,en", "en-CA", "Win10 en-CA"),
        (ua_win11, 1440, 812, 2.0, "Europe/London", "en-GB,en-US,en", "en-GB", "Win10 en-GB"),
        (ua_win11, 1920, 969, 1.0, "Europe/Berlin", "de-DE,de,en-US,en", "de-DE", "Win10 de-DE"),
        (ua_win11, 1920, 969, 1.0, "Europe/Paris", "fr-FR,fr,en-US,en", "fr-FR", "Win10 fr-FR"),
        (ua_win11, 1366, 642, 1.0, "America/New_York", "en-US,en", "en-US", "Win10 laptop 1366"),
        (ua_win11, 2560, 1271, 1.0, "America/Los_Angeles", "en-US,en", "en-US", "Win10 QHD"),
        (ua_mac, 1728, 994, 2.0, "America/Los_Angeles", "en-US,en", "en-US", "macOS Retina US"),
        (ua_mac, 1512, 857, 2.0, "America/New_York", "en-US,en", "en-US", "macOS Retina US East"),
        (ua_mac, 1440, 812, 2.0, "Europe/Amsterdam", "en-GB,en-US,en", "en-GB", "macOS EU"),
        (ua_mac, 1680, 939, 2.0, "Europe/Berlin", "de-DE,de,en-US,en", "de-DE", "macOS DE"),
        (ua_linux, 1920, 969, 1.0, "America/New_York", "en-US,en", "en-US", "Linux en-US"),
        (ua_linux, 1920, 969, 1.0, "Europe/Berlin", "en-US,en-GB,en", "en-US", "Linux EU"),
        (ua_linux, 1536, 754, 1.25, "America/Los_Angeles", "en-US,en", "en-US", "Linux laptop"),
        (ua_win, 1280, 720, 1.0, "America/Phoenix", "en-US,en", "en-US", "Win10 HD"),
        (ua_win11, 1920, 969, 1.1, "Australia/Sydney", "en-AU,en-US,en", "en-AU", "Win10 en-AU"),
        (ua_win11, 1920, 969, 1.0, "Asia/Singapore", "en-SG,en-US,en", "en-SG", "Win10 en-SG"),
    ]

    out: list[BrowserFingerprint] = []
    for ua, w, h, dpr, tz, langs, loc, label in raw:
        out.append(
            BrowserFingerprint(
                user_agent=ua,
                viewport_width=w,
                viewport_height=h,
                device_scale_factor=dpr,
                timezone_id=tz,
                accept_languages=langs,
                locale=loc,
                platform_label=label,
            )
        )
    return out


def pick_fingerprint(*, chrome_major: int) -> BrowserFingerprint:
    """Random coherent profile; Chrome major comes from installed browser."""
    major = max(100, min(200, int(chrome_major)))
    pool = _profiles_for_major(major)
    return secrets.choice(pool)


def apply_fingerprint(driver, fp: BrowserFingerprint) -> None:
    """Apply CDP overrides after driver is created (must run before first navigation if possible)."""
    try:
        driver.execute_cdp_cmd("Network.enable", {})
    except Exception:
        pass
    try:
        driver.execute_cdp_cmd(
            "Network.setUserAgentOverride",
            {"userAgent": fp.user_agent},
        )
    except Exception:
        pass
    try:
        driver.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": fp.viewport_width,
                "height": fp.viewport_height,
                "deviceScaleFactor": fp.device_scale_factor,
                "mobile": False,
            },
        )
    except Exception:
        pass
    try:
        driver.execute_cdp_cmd(
            "Emulation.setTimezoneOverride",
            {"timezoneId": fp.timezone_id},
        )
    except Exception:
        pass
    try:
        driver.execute_cdp_cmd(
            "Emulation.setLocaleOverride",
            {"locale": fp.locale},
        )
    except Exception:
        # Older Chrome may not support setLocaleOverride
        pass
