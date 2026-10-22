"""
Shared helpers
--------------
- YAHOO: one rate limiter for every Yahoo Finance call the site makes (Stocks page
  prices and metrics, and the ETF screen), so a busy page can't get the server
  blocked. After Yahoo answers "too many requests", all calls pause for a while.
- LRU: a dict with a size cap, so caches can't grow without limit.
"""

import os
import threading
import time
from collections import OrderedDict, deque

YAHOO_CALLS_PER_MIN = int(os.environ.get("YAHOO_CALLS_PER_MIN", "150"))
RATE_LIMIT_PAUSE = 90          # seconds to wait after Yahoo says "too many requests"


class RateLimiter:
    def __init__(self, per_minute):
        self.per_minute = per_minute
        self.lock = threading.Lock()
        self.calls = deque()
        self.paused_until = 0.0

    def try_acquire(self):
        """Take one call slot if there is one; never waits."""
        now = time.time()
        with self.lock:
            if now < self.paused_until:
                return False
            while self.calls and now - self.calls[0] > 60:
                self.calls.popleft()
            if len(self.calls) >= self.per_minute:
                return False
            self.calls.append(now)
            return True

    def acquire(self, timeout=60.0):
        """Wait up to `timeout` seconds for a call slot (used by background fetches)."""
        deadline = time.time() + timeout
        while True:
            if self.try_acquire():
                return True
            if time.time() >= deadline:
                return False
            time.sleep(0.25)

    def pause(self, seconds=RATE_LIMIT_PAUSE):
        with self.lock:
            self.paused_until = max(self.paused_until, time.time() + seconds)

    def paused(self):
        with self.lock:
            return time.time() < self.paused_until


YAHOO = RateLimiter(YAHOO_CALLS_PER_MIN)


class YahooBusy(Exception):
    """No Yahoo call slot came free in time."""


def is_rate_limit(exc):
    text = str(exc)
    return (type(exc).__name__ == "YFRateLimitError" or "Too Many Requests" in text
            or "Rate limited" in text or "429" in text)


def yahoo_call(fn, timeout=60.0):
    """Run one Yahoo request through the shared limiter. Waits for a slot; backs off on 429."""
    if not YAHOO.acquire(timeout):
        raise YahooBusy("Too many Yahoo requests right now")
    try:
        return fn()
    except Exception as e:
        if is_rate_limit(e):
            YAHOO.pause()
        raise


class LRU:
    """A dict that forgets its least recently used entries above `limit`.
    Not thread-safe on its own: callers hold their own lock."""

    def __init__(self, limit):
        self.limit = limit
        self.data = OrderedDict()

    def get(self, key, default=None):
        if key not in self.data:
            return default
        self.data.move_to_end(key)
        return self.data[key]

    def __getitem__(self, key):
        self.data.move_to_end(key)
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value
        self.data.move_to_end(key)
        while len(self.data) > self.limit:
            self.data.popitem(last=False)

    def __contains__(self, key):
        return key in self.data

    def __len__(self):
        return len(self.data)

    def pop(self, key, default=None):
        return self.data.pop(key, default)

    def clear(self):
        self.data.clear()
