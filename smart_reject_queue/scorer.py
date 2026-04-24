"""
Trust Scorer — Level 2, Component 3.

Converts 6 signal deviations into a single trust score 0–100.
  100 = completely trustworthy (legitimate reject)
    0 = definitely infra-induced (systemic false reject)

Each signal contributes a 0–1 penalty.  Penalties are weighted (rail-aware)
and summed to a total penalty, which is mapped linearly to trust score.

Classification thresholds (tunable via PolicyEvaluator):
  trust ≥ 75 → LEGITIMATE
  40 ≤ trust < 75 → SUSPECT
  trust < 40  → SYSTEMIC
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import RejectClass
from .signals import SignalReadout


# ---------------------------------------------------------------------------
# Signal Weights
# ---------------------------------------------------------------------------

@dataclass
class ScoringWeights:
    burst_ratio:        float = 0.30
    latency_ratio:      float = 0.20
    upstream_5xx:       float = 0.20
    cache_miss:         float = 0.15
    dependency_health:  float = 0.10
    cert_error:         float = 0.05


# Rail-specific weight profiles (one row per Rail value string)
_RAIL_WEIGHTS: dict[str, ScoringWeights] = {
    # Real-time rails: latency deviations matter most
    "RTP":   ScoringWeights(burst_ratio=0.25, latency_ratio=0.30, upstream_5xx=0.20,
                             cache_miss=0.10, dependency_health=0.10, cert_error=0.05),
    "ZELLE": ScoringWeights(burst_ratio=0.25, latency_ratio=0.30, upstream_5xx=0.20,
                             cache_miss=0.10, dependency_health=0.10, cert_error=0.05),
    # Wire: upstream health + 5xx rate most diagnostic
    "WIRE":  ScoringWeights(burst_ratio=0.25, latency_ratio=0.15, upstream_5xx=0.25,
                             cache_miss=0.15, dependency_health=0.15, cert_error=0.05),
    # ACH: burst ratio most diagnostic (batch volumes)
    "ACH":   ScoringWeights(burst_ratio=0.35, latency_ratio=0.15, upstream_5xx=0.20,
                             cache_miss=0.15, dependency_health=0.10, cert_error=0.05),
    # FX: dependency health critical (partner APIs for rate feeds)
    "FX":    ScoringWeights(burst_ratio=0.20, latency_ratio=0.20, upstream_5xx=0.20,
                             cache_miss=0.15, dependency_health=0.20, cert_error=0.05),
}

_DEFAULT_WEIGHTS = ScoringWeights()


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# Trust Scorer
# ---------------------------------------------------------------------------

class TrustScorer:
    """
    Weighted penalty model across 6 signals.
    Each signal maps to a 0–1 penalty via a normalisation function.
    Total weighted penalty → trust score 0–100.
    """

    def score(self, readout: SignalReadout, rail: str) -> tuple[int, list[str]]:
        """
        Returns (trust_score, signals_fired).
        signals_fired: list of strings describing each signal that meaningfully
                       penalised the score (for audit trail + human readability).
        """
        w = _RAIL_WEIGHTS.get(rail, _DEFAULT_WEIGHTS)
        fired: list[str] = []

        # ── Signal 1: burst ratio ──────────────────────────────────────────
        # 1.0 = normal  |  3.0 = 3× above p95 (strong systemic signal)
        burst_p = _clamp((readout.burst_ratio - 1.0) / 2.0)
        if burst_p > 0.15:
            fired.append(f"burst_ratio={readout.burst_ratio:.2f}×p95")

        # ── Signal 2: latency ratio ────────────────────────────────────────
        # 1.0 = at p99  |  2.5 = 2.5× p99 (strong timeout signal)
        latency_p = _clamp((readout.latency_ratio - 1.0) / 1.5)
        if latency_p > 0.15:
            fired.append(f"latency={readout.latency_ratio:.2f}×p99")

        # ── Signal 3: upstream 5xx excess ─────────────────────────────────
        # 0.15 excess fraction = full penalty
        fivexx_p = _clamp(readout.upstream_5xx_excess / 0.15)
        if fivexx_p > 0.10:
            fired.append(f"upstream_5xx_excess={readout.upstream_5xx_excess:.1%}")

        # ── Signal 4: cache miss excess ────────────────────────────────────
        cache_p = _clamp(readout.cache_miss_excess / 0.15)
        if cache_p > 0.10:
            fired.append(f"cache_miss_excess={readout.cache_miss_excess:.1%}")

        # ── Signal 5: dependency health ────────────────────────────────────
        # inverse already 0–1; normalise: 0.5 inverse = full penalty
        dep_p = _clamp(readout.dependency_health_inverse / 0.50)
        if dep_p > 0.10:
            health_pct = (1.0 - readout.dependency_health_inverse) * 100
            fired.append(f"dep_health={health_pct:.0f}%")

        # ── Signal 6: cert/auth error rate ─────────────────────────────────
        cert_p = _clamp(readout.cert_error_rate / 0.10)
        if cert_p > 0.10:
            fired.append(f"cert_error={readout.cert_error_rate:.1%}")

        total_penalty = (
            w.burst_ratio       * burst_p  +
            w.latency_ratio     * latency_p +
            w.upstream_5xx      * fivexx_p  +
            w.cache_miss        * cache_p   +
            w.dependency_health * dep_p     +
            w.cert_error        * cert_p
        )

        trust_score = round(100.0 * (1.0 - _clamp(total_penalty)))
        return trust_score, fired

    @staticmethod
    def classify(trust_score: int) -> tuple[RejectClass, float]:
        """
        Map trust score → (RejectClass, confidence 0–1).
        Confidence reflects how far into the classification band the score sits.
        """
        if trust_score >= 75:
            confidence = _clamp((trust_score - 75) / 25.0)
            return RejectClass.LEGITIMATE, confidence
        if trust_score >= 40:
            # Closer to 40 → more confident SUSPECT; closer to 75 → less confident
            confidence = _clamp((75 - trust_score) / 35.0)
            return RejectClass.SUSPECT, confidence
        confidence = _clamp((40 - trust_score) / 40.0)
        return RejectClass.SYSTEMIC, confidence
