"""Inbound request limiting: reject, don't delay.

An inbound limit must refuse, not delay. A limiter that `await`s until a slot is free is
the right shape for being polite to somebody else's service; for traffic arriving at this
one it is exactly wrong. An attacker guessing passwords is happy to wait, and every held
request is a connection, a thread and a database checkout that a real user does not get.
So this one refuses, with 429 and `Retry-After`, and it refuses before the request reaches
authentication — the point of the limit is to stop paying for the work, and
`JWTAuthMiddleware` costs a session lookup.

**What this does not solve.** The counters live in this process. Two uvicorn workers mean
two counters and therefore twice the configured limit; a horizontally scaled deployment
multiplies it by the replica count. The fix for that is a shared store (Redis), which is a
dependency this service does not have and would not otherwise need. Documented rather than
pretended: an in-process limit set to 10/minute still turns an unbounded credential-
stuffing run into a bounded one, which is the property that matters. The account lockout
in `controllers/auth_controller.py` (`AUTH_MAX_FAILED_LOGIN_ATTEMPTS`,
`AUTH_LOCKOUT_MINUTES`) is the per-account half of the same defence, and it is kept in the
database, so worker count does not affect it.
"""
from __future__ import annotations

import time
from collections import OrderedDict, deque


class SlidingWindowLimiter:
    """`limit` events per `window_seconds` per key, counted over a sliding window.

    A fixed window would let 2xlimit through across a boundary — 10 requests at 11:59:59
    and 10 more at 12:00:00 — which is precisely the burst a login limiter exists to stop.
    A deque of timestamps per key costs `limit` floats and is exact.

    Keys are client addresses, so the key space is attacker-controlled and has to be
    bounded: `max_keys` evicts least-recently-used. Eviction can only ever *forget* that
    somebody was near their limit, never invent budget for a key being actively used, because
    an active key is by definition recently used.
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        *,
        max_keys: int = 10_000,
        clock=time.monotonic,
    ) -> None:
        self.limit = max(0, int(limit))
        self.window = float(window_seconds)
        self.max_keys = max(1, int(max_keys))
        self._clock = clock
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def check(self, key: str) -> float | None:
        """Record a hit. Returns None if allowed, or the seconds to wait if refused.

        Refused hits are NOT recorded. Recording them would extend the block for as long
        as a client keeps retrying, which turns a rate limit into a lockout — a different
        control, with a different blast radius, that nobody asked for here.
        """
        if self.limit <= 0:  # 0 disables the limit; the caller decides what that means.
            return None

        now = self._clock()
        cutoff = now - self.window
        hits = self._hits.get(key)
        if hits is None:
            hits = deque()
            self._hits[key] = hits
        self._hits.move_to_end(key)

        while hits and hits[0] <= cutoff:
            hits.popleft()

        if len(hits) >= self.limit:
            return max(0.0, hits[0] + self.window - now)

        hits.append(now)

        while len(self._hits) > self.max_keys:
            self._hits.popitem(last=False)
        return None
