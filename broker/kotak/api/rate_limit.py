"""Account-wide request limiter for the Kotak Neo API.

Kotak publishes a single account-wide budget: **10 requests/second across all
APIs** (`broker-api-docs/kotak-api-docs/14-troubleshooting-faqs-error-codes.md`
Q24), and `429 Too many requests` is a documented error code
(`04-market-data-instruments.md` §5).

The budget is shared, so an uncounted burst of quote or funds calls can consume
the slot an order needs — and the order that fails is the one closing a
position. Before this module existed nothing under `broker/kotak/` counted
requests at all: `api/data.py` had a fixed sleep between multiquote batches and
the order path, funds and quotes were entirely uncounted.

A sliding window over the last second is enough. Kotak publishes no per-minute
figure, so a second window would be invented rather than derived from the
contract — unlike Flattrade, which documents both 10/sec and 40/min for orders
(`broker/flattrade/api/data.py#_reserve_rate_limit_slot` sizes two windows for
that reason).
"""

import threading
import time
from collections import deque

from utils.logging import get_logger

logger = get_logger(__name__)

# Kotak Neo Trade API v2: 10 requests/second across APIs.
KOTAK_MAX_REQUESTS_PER_SECOND = 10

# Timestamps (monotonic) of the slots reserved in the current window. A slot
# may be reserved for the FUTURE when the window is full, so the deque is
# ordered but not strictly "already sent".
_window: deque[float] = deque()
_window_lock = threading.Lock()


def reserve_slot(max_per_second: int = KOTAK_MAX_REQUESTS_PER_SECOND) -> float:
    """Reserve one slot in the current one-second window.

    Returns the number of seconds the caller must wait before sending. Zero
    means "send now". Reserving rather than merely checking is what keeps
    concurrent callers from all deciding the window has room at the same
    instant.
    """
    if max_per_second <= 0:
        return 0.0

    now = time.monotonic()
    with _window_lock:
        # Drop slots that have aged out of the window.
        while _window and (now - _window[0]) >= 1.0:
            _window.popleft()

        if len(_window) < max_per_second:
            _window.append(now)
            return 0.0

        # Window is full: the earliest slot frees at _window[0] + 1.0. Reserve
        # that future instant so the next caller queues behind this one instead
        # of computing the same wait and bursting together.
        wait = (_window[0] + 1.0) - now
        _window.popleft()
        _window.append(now + wait)
        return max(wait, 0.0)


def apply_rate_limit(max_per_second: int = KOTAK_MAX_REQUESTS_PER_SECOND) -> None:
    """Block until this caller may send, then return."""
    wait = reserve_slot(max_per_second)
    if wait > 0:
        logger.debug(f"Kotak rate limit: waiting {wait:.3f}s for a request slot")
        time.sleep(wait)


def reset_rate_limit() -> None:
    """Drop the window (test / administrative helper)."""
    with _window_lock:
        _window.clear()
