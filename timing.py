"""Request-scoped timing. Thread-local so concurrent requests don't collide."""
from __future__ import annotations
import threading
import time

_state = threading.local()


def init_timing() -> None:
    """Start a timing window on the current thread."""
    _state.start = time.monotonic()
    _state.entries = []


def record_timing(label: str) -> None:
    """Record elapsed_ms since the last mark (or since init). Silent no-op if not initialized."""
    start = getattr(_state, "start", None)
    if start is None:
        return
    now = time.monotonic()
    elapsed_ms = int((now - start) * 1000)
    _state.entries.append({"label": label, "elapsed_ms": elapsed_ms})
    _state.start = now


def get_timings() -> list[dict]:
    """Return recorded entries and reset thread-local state. Returns [] if never initialized."""
    entries = getattr(_state, "entries", [])
    _state.start = None
    _state.entries = []
    return entries
