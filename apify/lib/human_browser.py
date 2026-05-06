"""CDP-based smooth pointer motion and human-paced typing (shared by Apollo scripts)."""

import math
import random
import time

from selenium.webdriver.common.action_chains import ActionChains

# Last synthetic pointer position for smooth CDP moves (viewport coordinates).
_MOUSE = {"x": None, "y": None}


def reset_synthetic_mouse() -> None:
    _MOUSE["x"] = _MOUSE["y"] = None


def _rng_pause(lo: float, hi: float) -> None:
    time.sleep(random.uniform(lo, hi))


def _viewport_size(driver):
    return driver.execute_script(
        "return {w: window.innerWidth, h: window.innerHeight};"
    )


def _ensure_mouse_origin(driver) -> None:
    if _MOUSE["x"] is None:
        v = _viewport_size(driver)
        _MOUSE["x"] = v["w"] / 2
        _MOUSE["y"] = v["h"] / 2


def _ease_in_out_cubic(t: float) -> float:
    return 4 * t * t * t if t < 0.5 else 1 - pow(-2 * t + 2, 3) / 2


def _cdp_mouse_moved(driver, x: float, y: float) -> None:
    driver.execute_cdp_cmd(
        "Input.dispatchMouseEvent",
        {"type": "mouseMoved", "x": int(round(x)), "y": int(round(y))},
    )


def _human_curve_move(driver, x0, y0, x1, y1, duration: float) -> None:
    """Bezier-ish path with slight perpendicular bend and jitter (feels less robotic)."""
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy) or 1.0
    px, py = -dy / length, dx / length
    cap = min(90.0, length * 0.35)
    offset = random.uniform(-cap, cap)
    cx, cy = mx + px * offset, my + py * offset

    steps = max(12, min(85, int(duration * (50 + random.randint(-8, 12)))))
    for i in range(1, steps + 1):
        t = i / steps
        te = _ease_in_out_cubic(t)
        omt = 1 - te
        x = omt * omt * x0 + 2 * omt * te * cx + te * te * x1
        y = omt * omt * y0 + 2 * omt * te * cy + te * te * y1
        x += random.uniform(-1.8, 1.8)
        y += random.uniform(-1.8, 1.8)
        _cdp_mouse_moved(driver, x, y)
        time.sleep((duration / steps) * random.uniform(0.82, 1.18))
    _MOUSE["x"], _MOUSE["y"] = x1, y1


def _curve_move_smooth(driver, x0: float, y0: float, x1: float, y1: float, duration: float) -> None:
    """
    Same quadratic Bezier as _human_curve_move but **no random jitter** — smooth, repeatable path.
    Bend uses a fixed fraction of the perpendicular cap (reads as intentional hand arc, not noise).
    """
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy) or 1.0
    px, py = -dy / length, dx / length
    cap = min(90.0, length * 0.35)
    offset = cap * 0.36
    cx, cy = mx + px * offset, my + py * offset
    steps = max(14, min(78, int(duration * 52)))
    step_sleep = duration / steps
    for i in range(1, steps + 1):
        t = i / steps
        te = _ease_in_out_cubic(t)
        omt = 1 - te
        x = omt * omt * x0 + 2 * omt * te * cx + te * te * x1
        y = omt * omt * y0 + 2 * omt * te * cy + te * te * y1
        _cdp_mouse_moved(driver, x, y)
        time.sleep(step_sleep)
    _MOUSE["x"], _MOUSE["y"] = x1, y1


def human_move_to_coordinate_smooth(driver, x1: float, y1: float, duration: float | None = None) -> None:
    """Smooth move without randomness; duration scales with distance if omitted."""
    _ensure_mouse_origin(driver)
    x0, y0 = _MOUSE["x"], _MOUSE["y"]
    if duration is None:
        dist = math.hypot(x1 - x0, y1 - y0)
        duration = 0.22 + min(1.15, (dist / 920.0) * 1.05)
    _curve_move_smooth(driver, x0, y0, x1, y1, duration)


def _micro_jiggle_smooth(driver, x: float, y: float) -> None:
    """Small pre-click pointer adjustments (deterministic, not random)."""
    v = _viewport_size(driver)
    for i in range(1, 4):
        jx = max(3.0, min(v["w"] - 3.0, x + 2.4 * math.sin(i * 1.12)))
        jy = max(3.0, min(v["h"] - 3.0, y + 2.1 * math.cos(i * 0.93)))
        _cdp_mouse_moved(driver, jx, jy)
        time.sleep(0.032 + i * 0.011)
    _cdp_mouse_moved(driver, x, y)
    time.sleep(0.07)


def human_click_smooth(driver, element) -> None:
    """
    Move along a smooth curve to a **deterministic** point inside the element, pause, then click.
    Intended for flows that should avoid heavy randomness (e.g. Apify automation).
    """
    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", element
    )
    time.sleep(0.26)
    box = driver.execute_script(
        """
        const r = arguments[0].getBoundingClientRect();
        return {cx: r.left + r.width/2, cy: r.top + r.height/2, w: r.width, h: r.height};
        """,
        element,
    )
    v = _viewport_size(driver)
    w, h = float(box["w"]), float(box["h"])
    cx, cy = float(box["cx"]), float(box["cy"])
    if w < 2 or h < 2:
        x = max(3, min(v["w"] - 3, cx))
        y = max(3, min(v["h"] - 3, cy))
    else:
        ox = math.sin(math.fmod(cx, 220.0) * 0.065) * min(w * 0.16, 20.0)
        oy = math.cos(math.fmod(cy, 190.0) * 0.072) * min(h * 0.14, 16.0)
        x = max(3, min(v["w"] - 3, cx + ox))
        y = max(3, min(v["h"] - 3, cy + oy))
    human_move_to_coordinate_smooth(driver, x, y)
    time.sleep(0.22)
    _micro_jiggle_smooth(driver, x, y)
    time.sleep(0.12)
    xi, yi = int(round(x)), int(round(y))
    driver.execute_cdp_cmd(
        "Input.dispatchMouseEvent",
        {
            "type": "mousePressed",
            "x": xi,
            "y": yi,
            "button": "left",
            "buttons": 1,
            "clickCount": 1,
        },
    )
    time.sleep(0.1)
    xrel = xi + (1 if xi % 2 == 0 else -1)
    yrel = yi + (1 if yi % 2 == 0 else -1)
    xrel = max(3, min(int(v["w"]) - 3, xrel))
    yrel = max(3, min(int(v["h"]) - 3, yrel))
    driver.execute_cdp_cmd(
        "Input.dispatchMouseEvent",
        {
            "type": "mouseReleased",
            "x": xrel,
            "y": yrel,
            "button": "left",
            "buttons": 0,
            "clickCount": 1,
        },
    )
    _MOUSE["x"], _MOUSE["y"] = float(xrel), float(yrel)


def human_move_to_coordinate(driver, x1, y1, duration=None) -> None:
    """Smooth move to viewport (x1, y1). Duration scales with distance if omitted."""
    _ensure_mouse_origin(driver)
    x0, y0 = _MOUSE["x"], _MOUSE["y"]
    if duration is None:
        dist = math.hypot(x1 - x0, y1 - y0)
        duration = 0.18 + min(1.25, (dist / 900.0) * random.uniform(0.85, 1.25))
    _human_curve_move(driver, x0, y0, x1, y1, duration)


def human_wander_viewport(driver, total_sec: float, inner_frac: float = 0.32) -> None:
    """Slow moves in the central band while the page loads (reading-like motion)."""
    if total_sec <= 0:
        return
    v = _viewport_size(driver)
    w0, w1 = v["w"] * inner_frac, v["w"] * (1 - inner_frac)
    h0, h1 = v["h"] * inner_frac, v["h"] * (1 - inner_frac)
    end = time.time() + total_sec
    while time.time() < end:
        remaining = end - time.time()
        if remaining < 0.06:
            break
        tx = random.uniform(w0, w1)
        ty = random.uniform(h0, h1)
        seg = min(remaining * random.uniform(0.45, 0.92), random.uniform(0.3, 1.05))
        human_move_to_coordinate(driver, tx, ty, duration=seg)
        _rng_pause(0.06, 0.32)


def _element_center_viewport(driver, element):
    return driver.execute_script(
        """
        const r = arguments[0].getBoundingClientRect();
        return {x: r.left + r.width/2, y: r.top + r.height/2};
        """,
        element,
    )


def click_element_simple(driver, element) -> None:
    """Scroll into view and click (no CDP curve / random viewport motion)."""
    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", element
    )
    time.sleep(0.08)
    try:
        element.click()
    except Exception:
        driver.execute_script("arguments[0].click();", element)


def human_actionchains_click_element(driver, element) -> None:
    """
    W3C ActionChains move + click with a random point inside the element.
    Prefer this inside iframes (CDP viewport coords differ from the main frame).
    """
    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", element
    )
    _rng_pause(0.22, 0.65)
    r = driver.execute_script(
        "const r=arguments[0].getBoundingClientRect(); return {w:r.width,h:r.height};",
        element,
    )
    w, h = float(r["w"]), float(r["h"])
    if w < 4.0 or h < 4.0:
        human_click_element(driver, element)

        return
    ox = w * 0.5 + random.uniform(-w * 0.22, w * 0.22)
    oy = h * 0.5 + random.uniform(-h * 0.22, h * 0.22)
    ox = max(3.0, min(w - 3.0, ox))
    oy = max(3.0, min(h - 3.0, oy))
    ActionChains(driver).move_to_element_with_offset(
        element, int(ox), int(oy)
    ).pause(random.uniform(0.12, 0.42)).click().pause(random.uniform(0.06, 0.2)).perform()


def human_click_element(driver, element) -> None:
    """Move smoothly to the control, pause, then CDP click (avoids instant .click())."""
    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", element
    )
    _rng_pause(0.12, 0.38)
    box = driver.execute_script(
        """
        const r = arguments[0].getBoundingClientRect();
        return {cx: r.left + r.width/2, cy: r.top + r.height/2, w: r.width, h: r.height};
        """,
        element,
    )
    v = _viewport_size(driver)
    w, h = float(box["w"]), float(box["h"])
    if w < 2 or h < 2:
        c = _element_center_viewport(driver, element)
        x = max(3, min(v["w"] - 3, c["x"]))
        y = max(3, min(v["h"] - 3, c["y"]))
    else:
        ox = random.uniform(-min(w * 0.22, 28), min(w * 0.22, 28))
        oy = random.uniform(-min(h * 0.22, 22), min(h * 0.22, 22))
        x = max(3, min(v["w"] - 3, box["cx"] + ox))
        y = max(3, min(v["h"] - 3, box["cy"] + oy))
    human_move_to_coordinate(driver, x, y)
    _rng_pause(0.18, 0.62)
    human_micro_jiggle_viewport(driver, x, y)
    _rng_pause(0.08, 0.35)
    xi, yi = int(round(x)), int(round(y))
    driver.execute_cdp_cmd(
        "Input.dispatchMouseEvent",
        {
            "type": "mousePressed",
            "x": xi,
            "y": yi,
            "button": "left",
            "buttons": 1,
            "clickCount": 1,
        },
    )
    _rng_pause(0.06, 0.18)
    xrel = xi + random.randint(-1, 1)
    yrel = yi + random.randint(-1, 1)
    driver.execute_cdp_cmd(
        "Input.dispatchMouseEvent",
        {
            "type": "mouseReleased",
            "x": xrel,
            "y": yrel,
            "button": "left",
            "buttons": 0,
            "clickCount": 1,
        },
    )
    _MOUSE["x"], _MOUSE["y"] = float(xrel), float(yrel)


def human_micro_jiggle_viewport(driver, x: float, y: float) -> None:
    """A few tiny pointer moves before a click (reduces perfectly static bot coordinates)."""
    v = _viewport_size(driver)
    for _ in range(random.randint(2, 5)):
        jx = max(3.0, min(v["w"] - 3.0, x + random.uniform(-4.5, 4.5)))
        jy = max(3.0, min(v["h"] - 3.0, y + random.uniform(-4.5, 4.5)))
        _cdp_mouse_moved(driver, jx, jy)
        _rng_pause(0.028, 0.09)
    _cdp_mouse_moved(driver, x, y)
    _rng_pause(0.05, 0.18)


def human_slow_click_viewport(
    driver, x: float, y: float, *, move_duration: float | None = None
) -> None:
    """
    Slow move then left-click at viewport (x, y). Use when the target is inside closed
    shadow DOM (e.g. Cloudflare Turnstile) and only hit-testing / compositor sees it.
    """
    v = _viewport_size(driver)
    x = max(3.0, min(v["w"] - 3.0, float(x)))
    y = max(3.0, min(v["h"] - 3.0, float(y)))
    if move_duration is None:
        move_duration = random.uniform(0.75, 1.55)
    human_move_to_coordinate(driver, x, y, duration=move_duration)
    _rng_pause(0.45, 1.35)
    human_micro_jiggle_viewport(driver, x, y)
    _rng_pause(0.12, 0.55)
    xi = int(round(x)) + random.randint(-1, 1)
    yi = int(round(y)) + random.randint(-1, 1)
    xi = max(3, min(int(v["w"]) - 3, xi))
    yi = max(3, min(int(v["h"]) - 3, yi))
    driver.execute_cdp_cmd(
        "Input.dispatchMouseEvent",
        {
            "type": "mousePressed",
            "x": xi,
            "y": yi,
            "button": "left",
            "buttons": 1,
            "clickCount": 1,
        },
    )
    _rng_pause(0.09, 0.28)
    xrel = xi + random.randint(-1, 1)
    yrel = yi + random.randint(-1, 1)
    xrel = max(3, min(int(v["w"]) - 3, xrel))
    yrel = max(3, min(int(v["h"]) - 3, yrel))
    driver.execute_cdp_cmd(
        "Input.dispatchMouseEvent",
        {
            "type": "mouseReleased",
            "x": xrel,
            "y": yrel,
            "button": "left",
            "buttons": 0,
            "clickCount": 1,
        },
    )
    _MOUSE["x"], _MOUSE["y"] = float(xrel), float(yrel)


def human_type_text(element, text: str) -> None:
    """Character-by-character typing with variable gaps (tuned faster)."""
    for ch in text:
        element.send_keys(ch)
        time.sleep(random.uniform(0.012, 0.045))


def human_type_text_slow(element, text: str) -> None:
    """
    Slower, more deliberate typing for sensitive flows (login, signup).
    Gaps vary gently by position (deterministic), not heavy random jitter.
    """
    for i, ch in enumerate(text):
        element.send_keys(ch)
        base = 0.048 + 0.032 * (0.5 + 0.5 * math.sin(i * 0.73))
        time.sleep(min(0.12, max(0.028, base)))
