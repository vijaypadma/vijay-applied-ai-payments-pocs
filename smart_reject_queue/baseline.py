"""
Baseline Cache (Level 4) + Baseline Comparator (Level 2, Component 2).

BaselineCache   — in-memory simulation of Redis/GemFire.
                  Pre-warmed at deploy with 90-day p50/p95/p99 per rail+code.
                  All reads < 0.1ms (pure dict lookup).

BaselineComparator — converts live signal values into deviation ratios
                     that the Trust Scorer can consume directly.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Baseline Entry
# ---------------------------------------------------------------------------

@dataclass
class BaselineEntry:
    """Historical percentile stats for one (rail, reject_code) combination."""
    rail:               str
    reject_code:        str
    reject_rate_p50:    float   # rejects / min at p50
    reject_rate_p95:    float   # rejects / min at p95
    reject_rate_p99:    float   # rejects / min at p99
    latency_p50_ms:     float
    latency_p99_ms:     float
    cache_miss_p95:     float   # expected cache-miss fraction at p95
    upstream_5xx_p95:   float   # expected upstream-5xx fraction at p95


# ---------------------------------------------------------------------------
# Rolling Rate Tracker  (used by Baseline Comparator)
# ---------------------------------------------------------------------------

class RollingRateTracker:
    """Count events in a rolling time window; compute rate per minute."""

    def __init__(self, window_seconds: int = 60):
        self._window = window_seconds
        self._events: deque[float] = deque()

    def record(self, ts: float | None = None) -> None:
        t = ts if ts is not None else time.time()
        self._events.append(t)
        self._evict(t)

    def _evict(self, now: float) -> None:
        cutoff = now - self._window
        while self._events and self._events[0] < cutoff:
            self._events.popleft()

    def rate_per_minute(self, now: float | None = None) -> float:
        self._evict(now if now is not None else time.time())
        return len(self._events) / (self._window / 60.0)

    def count(self) -> int:
        self._evict(time.time())
        return len(self._events)


# ---------------------------------------------------------------------------
# Baseline Cache
# ---------------------------------------------------------------------------

class BaselineCache:
    """
    Simulates Redis baseline cache pre-warmed at deploy time.
    Production implementation replaces _store lookups with Redis GET calls
    that return serialised BaselineEntry JSON.  All reads must remain < 0.1ms,
    so the SDK uses a local cache-client (e.g. Lettuce / Jedis with connection
    pooling) — never a raw network hop in the reject path.
    """

    def __init__(self):
        self._store: dict[tuple[str, str], BaselineEntry] = {}
        self._warm()

    def _warm(self) -> None:
        """
        Load 90-day p50/p95/p99 stats per (rail, reject_code).
        Values below are representative production baselines.
        """
        entries: list[BaselineEntry] = [
            # ACH
            BaselineEntry("ACH",   "R01",  2.0,  4.0,  6.0,   80,  200, 0.05, 0.010),
            BaselineEntry("ACH",   "R02",  0.5,  1.0,  2.0,   80,  200, 0.05, 0.010),
            BaselineEntry("ACH",   "R03",  0.3,  0.6,  1.0,   80,  200, 0.05, 0.010),
            BaselineEntry("ACH",   "R29",  0.2,  0.4,  0.8,   80,  200, 0.05, 0.010),
            # WIRE (SWIFT ISO 20022 codes)
            BaselineEntry("WIRE",  "AC04", 0.5,  1.0,  2.0,  120,  400, 0.03, 0.010),
            BaselineEntry("WIRE",  "AM04", 0.3,  0.8,  1.5,  120,  400, 0.03, 0.010),
            BaselineEntry("WIRE",  "RR01", 0.1,  0.3,  0.5,  120,  400, 0.03, 0.010),
            BaselineEntry("WIRE",  "AC01", 0.2,  0.5,  0.9,  120,  400, 0.03, 0.010),
            # RTP
            BaselineEntry("RTP",   "R01",  1.0,  2.0,  3.0,   20,   60, 0.02, 0.005),
            BaselineEntry("RTP",   "R03",  0.4,  0.8,  1.5,   20,   60, 0.02, 0.005),
            # ZELLE
            BaselineEntry("ZELLE", "R01",  0.8,  1.5,  2.5,   25,   70, 0.02, 0.005),
            # FX
            BaselineEntry("FX",    "AM03", 0.2,  0.5,  0.9,  200,  600, 0.04, 0.010),
            BaselineEntry("FX",    "TM01", 0.1,  0.3,  0.5,  200,  600, 0.04, 0.010),
        ]
        for e in entries:
            self._store[(e.rail, e.reject_code)] = e

    def get(self, rail: str, code: str) -> BaselineEntry | None:
        return self._store.get((rail, code))

    def get_or_default(self, rail: str, code: str) -> BaselineEntry:
        return self._store.get((rail, code)) or BaselineEntry(
            rail, code,
            reject_rate_p50=1.0, reject_rate_p95=2.0, reject_rate_p99=3.0,
            latency_p50_ms=100,  latency_p99_ms=300,
            cache_miss_p95=0.05, upstream_5xx_p95=0.02,
        )


# ---------------------------------------------------------------------------
# Baseline Comparator
# ---------------------------------------------------------------------------

class BaselineComparator:
    """
    Turns live signal values into dimensionless deviation ratios.
    The Trust Scorer consumes these ratios — never the raw values.
    """

    def __init__(self, cache: BaselineCache):
        self._cache = cache
        self._trackers: dict[tuple[str, str], RollingRateTracker] = {}

    # ── internal ────────────────────────────────────────────────────────────

    def _tracker(self, rail: str, code: str) -> RollingRateTracker:
        key = (rail, code)
        if key not in self._trackers:
            self._trackers[key] = RollingRateTracker(window_seconds=60)
        return self._trackers[key]

    # ── public ───────────────────────────────────────────────────────────────

    def record_reject(self, rail: str, code: str, ts: float | None = None) -> None:
        """Call once per reject candidate before computing deviations."""
        self._tracker(rail, code).record(ts)

    def burst_ratio(self, rail: str, code: str) -> float:
        """current_rate / p95_baseline; 1.0 = normal, 3.0 = 3× above baseline."""
        b = self._cache.get_or_default(rail, code)
        current = self._tracker(rail, code).rate_per_minute()
        return current / (b.reject_rate_p95 or 1.0)

    def latency_ratio(self, latency_ms: float, rail: str, code: str) -> float:
        """latency / p99_baseline; 1.0 = at p99, 2.0 = 2× p99."""
        b = self._cache.get_or_default(rail, code)
        return latency_ms / (b.latency_p99_ms or 100.0)

    def cache_miss_excess(self, miss_rate: float, rail: str, code: str) -> float:
        """How far above the p95 cache-miss baseline (clamped to ≥ 0)."""
        b = self._cache.get_or_default(rail, code)
        return max(0.0, miss_rate - b.cache_miss_p95)

    def upstream_5xx_excess(self, rate_5xx: float, rail: str, code: str) -> float:
        """How far above the p95 upstream-5xx baseline (clamped to ≥ 0)."""
        b = self._cache.get_or_default(rail, code)
        return max(0.0, rate_5xx - b.upstream_5xx_p95)
