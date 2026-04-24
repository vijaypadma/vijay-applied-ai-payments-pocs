"""
Signal Collector — Level 2, Component 1.

Reads 6 runtime infrastructure signals at reject time.
All values come from in-process context or local cache client.
Zero external I/O.  Target: < 0.2ms total.

The 6 signals (per SRQ architecture):
  1. reject_burst_ratio       — current rate vs p95 baseline
  2. latency_ratio            — processing latency vs p99 baseline
  3. upstream_5xx_excess      — excess 5xx rate above baseline
  4. cache_miss_excess        — excess cache-miss rate above baseline
  5. dependency_health_inverse — 1 − health_score  (0 = healthy, 1 = down)
  6. cert_error_rate          — raw fraction of cert/auth errors
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import RejectCandidate
from .baseline import BaselineComparator


@dataclass
class SignalReadout:
    """
    Dimensionless deviation scores for all 6 signals.
    Fed directly into the Trust Scorer.
    All values ≥ 0; higher = worse infrastructure health.
    """
    burst_ratio:               float  # current_rate / p95_baseline   (1.0 = normal)
    latency_ratio:             float  # latency / p99_baseline         (1.0 = at p99)
    upstream_5xx_excess:       float  # fraction above p95 5xx baseline
    cache_miss_excess:         float  # fraction above p95 miss baseline
    dependency_health_inverse: float  # 1 − health_score
    cert_error_rate:           float  # raw cert/auth error fraction


class SignalCollector:
    """
    Reads 6 runtime signals for a RejectCandidate and returns a SignalReadout.
    Depends on BaselineComparator for deviation calculations.
    """

    def __init__(self, comparator: BaselineComparator):
        self._cmp = comparator

    def collect(self, candidate: RejectCandidate) -> SignalReadout:
        rail = candidate.rail.value
        code = candidate.reject_reason_code
        sig  = candidate.signals

        # Register this reject in the rolling rate tracker (feeds burst_ratio)
        self._cmp.record_reject(rail, code)

        return SignalReadout(
            burst_ratio               = self._cmp.burst_ratio(rail, code),
            latency_ratio             = self._cmp.latency_ratio(candidate.processing_latency_ms, rail, code),
            upstream_5xx_excess       = self._cmp.upstream_5xx_excess(sig.upstream_5xx_rate, rail, code),
            cache_miss_excess         = self._cmp.cache_miss_excess(sig.cache_miss_rate, rail, code),
            dependency_health_inverse = 1.0 - sig.dependency_health_score,
            cert_error_rate           = sig.cert_error_rate,
        )
