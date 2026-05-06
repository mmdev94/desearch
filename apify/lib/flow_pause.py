"""
Press Space in the terminal to pause/resume automation (game-style: freezes between actions).

Requires a POSIX TTY on stdin (typical: `python apify/launch_final.py` in a terminal).
When stdin is not a TTY (e.g. CI), the listener is not started.

Use ``pause_aware_sleep`` instead of ``time.sleep`` so a pause takes effect during long waits;
``checkpoint()`` alone only runs at call sites — sleeps would otherwise ignore pause.
"""

from __future__ import annotations

import atexit
import sys
import threading
import time

try:
    import termios
    import tty

    _HAS_TERMIOS = True
except ImportError:
    _HAS_TERMIOS = False

_paused = threading.Event()
_listener_started = False
_listener_lock = threading.Lock()
_stdin_fd: int | None = None
_saved_termios: list | None = None
_atexit_registered = False


def is_paused() -> bool:
    return _paused.is_set()


def checkpoint() -> None:
    """Block while the user has paused (Space) the flow."""
    while _paused.is_set():
        time.sleep(0.12)


def pause_aware_sleep(seconds: float, *, chunk: float = 0.12) -> None:
    """
    Sleep up to ``seconds`` wall time, but block first whenever Space-pause is active.

    Long sleeps are split into ``chunk`` steps so pause takes effect within ~chunk seconds.
    """
    total = max(0.0, float(seconds))
    while True:
        checkpoint()
        if total <= 1e-9:
            return
        step = min(float(chunk), total)
        time.sleep(step)
        total -= step


def _restore_tty() -> None:
    global _saved_termios, _stdin_fd
    if not _HAS_TERMIOS or _saved_termios is None or _stdin_fd is None:
        return
    try:
        termios.tcsetattr(_stdin_fd, termios.TCSADRAIN, _saved_termios)
    except Exception:
        pass
    _saved_termios = None
    _stdin_fd = None


def _toggle() -> None:
    if _paused.is_set():
        _paused.clear()
        print("\n[flow-pause] Resumed.", flush=True)
    else:
        _paused.set()
        print("\n[flow-pause] Paused — press Space again to resume.", flush=True)


def _listener_main() -> None:
    global _saved_termios, _stdin_fd, _atexit_registered
    fd = sys.stdin.fileno()
    try:
        old = termios.tcgetattr(fd)
    except Exception:
        return
    _stdin_fd = fd
    _saved_termios = old
    if not _atexit_registered:
        atexit.register(_restore_tty)
        _atexit_registered = True
    try:
        tty.setcbreak(fd)
        while True:
            try:
                ch = sys.stdin.read(1)
            except Exception:
                break
            if ch == " ":
                _toggle()
    finally:
        _restore_tty()


def start_flow_pause_listener(*, enabled: bool = True) -> None:
    """Start background thread reading Space on stdin. No-op if disabled or not a TTY."""
    global _listener_started
    if not enabled:
        return
    if not _HAS_TERMIOS:
        print("[flow-pause] Space pause unavailable (POSIX termios not available).", flush=True)
        return
    if not sys.stdin.isatty():
        print("[flow-pause] Space pause unavailable (stdin is not a terminal).", flush=True)
        return
    with _listener_lock:
        if _listener_started:
            return
        t = threading.Thread(target=_listener_main, name="flow-pause-listener", daemon=True)
        t.start()
        _listener_started = True
    print(
        "[flow-pause] Space = pause/resume (game-style: waits and signup/scrape steps honor pause).",
        flush=True,
    )
