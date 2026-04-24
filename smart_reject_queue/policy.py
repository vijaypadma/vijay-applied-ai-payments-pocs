"""
Policy Evaluator — Level 2, Component 4.
Policy Governor  — Level 3, Component 5.

PolicyEvaluator: synchronous, in-process.
  Per-rail thresholds and per-client overrides.
  Reads from in-memory policy store (production: Redis, < 0.1ms).
  Conservative defaults: only quarantine when confidence is high.

PolicyGovernor:  control-plane component.
  Manages replay-window registry and runtime policy updates.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Rail Policy
# ---------------------------------------------------------------------------

@dataclass
class RailPolicy:
    """Quarantine rules for one rail.  All fields are runtime-configurable."""
    rail:                  str
    quarantine_threshold:  int   = 60     # trust score below this → QUARANTINE
    min_confidence:        float = 0.50   # minimum confidence required to act
    enabled:               bool  = True   # kill switch — set False to disable SRQ on this rail


# ---------------------------------------------------------------------------
# Client Policy  (per-client overrides)
# ---------------------------------------------------------------------------

@dataclass
class ClientPolicy:
    """Optional per-client threshold override.  None = use rail default."""
    client_id:                       str
    quarantine_threshold_override:   int | None  = None
    enabled:                         bool        = True


# ---------------------------------------------------------------------------
# Default Rail Policies
# Conservative starting point per the PDF:
# "Start conservative — only quarantine when you're very confident."
# ---------------------------------------------------------------------------

DEFAULT_RAIL_POLICIES: dict[str, RailPolicy] = {
    "ACH":   RailPolicy("ACH",   quarantine_threshold=55, min_confidence=0.50),
    "WIRE":  RailPolicy("WIRE",  quarantine_threshold=60, min_confidence=0.55),
    "RTP":   RailPolicy("RTP",   quarantine_threshold=65, min_confidence=0.60),
    "ZELLE": RailPolicy("ZELLE", quarantine_threshold=65, min_confidence=0.60),
    "FX":    RailPolicy("FX",    quarantine_threshold=58, min_confidence=0.55),
}


# ---------------------------------------------------------------------------
# Policy Evaluator  (Level 2 — synchronous, in-process)
# ---------------------------------------------------------------------------

from .models import TrustAction, RejectClass


class PolicyEvaluator:
    """
    Decides QUARANTINE vs PASS.
    Inputs: trust_score, classification, confidence, rail, optional client_id.
    Output: (TrustAction, human-readable reason).
    """

    def __init__(
        self,
        rail_policies:   dict[str, RailPolicy]   | None = None,
        client_policies: dict[str, ClientPolicy] | None = None,
    ):
        self._rail:   dict[str, RailPolicy]   = rail_policies   or dict(DEFAULT_RAIL_POLICIES)
        self._client: dict[str, ClientPolicy] = client_policies or {}

    def evaluate(
        self,
        trust_score:    int,
        classification: RejectClass,
        confidence:     float,
        rail:           str,
        client_id:      str = "",
    ) -> tuple[TrustAction, str]:

        rp = self._rail.get(rail, RailPolicy(rail))

        if not rp.enabled:
            return TrustAction.PASS, f"SRQ disabled for rail={rail}"

        threshold = rp.quarantine_threshold
        min_conf  = rp.min_confidence

        # Apply per-client override if present
        cp = self._client.get(client_id)
        if cp and cp.enabled and cp.quarantine_threshold_override is not None:
            threshold = cp.quarantine_threshold_override

        if trust_score < threshold and confidence >= min_conf:
            return (
                TrustAction.QUARANTINE,
                f"trust={trust_score} < threshold={threshold}, "
                f"conf={confidence:.0%}, class={classification.value}",
            )

        return (
            TrustAction.PASS,
            f"trust={trust_score} >= threshold={threshold} — legitimate reject",
        )

    # ── Runtime updates (called by PolicyGovernor) ───────────────────────

    def update_rail(self, policy: RailPolicy) -> None:
        self._rail[policy.rail] = policy

    def update_client(self, policy: ClientPolicy) -> None:
        self._client[policy.client_id] = policy


# ---------------------------------------------------------------------------
# Policy Governor  (Level 3 — control plane)
# ---------------------------------------------------------------------------

@dataclass
class ReplayWindow:
    """Rail-specific replay timing constraints."""
    rail:                 str
    max_hold_seconds:     int    # escalate to ops if not resolved within this window
    canary_pct:           float  = 0.10   # 10% canary first per PDF
    canary_success_min:   float  = 0.95   # ≥ 95% required per PDF


_DEFAULT_REPLAY_WINDOWS: dict[str, ReplayWindow] = {
    "ACH":   ReplayWindow("ACH",   86_400, canary_pct=0.10, canary_success_min=0.95),
    "WIRE":  ReplayWindow("WIRE",   3_600, canary_pct=0.10, canary_success_min=0.95),
    "RTP":   ReplayWindow("RTP",      300, canary_pct=0.10, canary_success_min=0.95),
    "ZELLE": ReplayWindow("ZELLE",    300, canary_pct=0.10, canary_success_min=0.95),
    "FX":    ReplayWindow("FX",     1_800, canary_pct=0.10, canary_success_min=0.95),
}


class PolicyGovernor:
    """
    Runtime-configurable policy store for the control plane.
    Manages rail policies and the replay-window registry.
    Pushes updates to the embedded PolicyEvaluator instances.
    """

    def __init__(self, evaluator: PolicyEvaluator | None = None):
        self._evaluator = evaluator
        self._replay_windows: dict[str, ReplayWindow] = dict(_DEFAULT_REPLAY_WINDOWS)

    def set_evaluator(self, evaluator: PolicyEvaluator) -> None:
        self._evaluator = evaluator

    def get_replay_window(self, rail: str) -> ReplayWindow:
        return self._replay_windows.get(rail, ReplayWindow(rail, max_hold_seconds=3_600))

    def update_rail_policy(self, policy: RailPolicy) -> None:
        if self._evaluator:
            self._evaluator.update_rail(policy)

    def update_client_policy(self, policy: ClientPolicy) -> None:
        if self._evaluator:
            self._evaluator.update_client(policy)

    def update_replay_window(self, window: ReplayWindow) -> None:
        self._replay_windows[window.rail] = window
