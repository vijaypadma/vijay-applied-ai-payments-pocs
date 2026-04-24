"""
SRQ SDK — Level 1 + Level 2.

The embedded library imported by payment microservices.
Intercepts every reject decision synchronously, in-process,
before any state is written or any client notification is sent.

Usage (in a payment service):

    from meridian_smart_queue import SRQSDK, TrustAction

    sdk = SRQSDK(quarantine_publisher=my_event_bus.publish)

    decision = sdk.evaluate(candidate)

    if decision.action == TrustAction.QUARANTINE:
        # Do NOT write REJECT_FINAL
        # Set external state to PROCESSING_DELAYED
        ...
    else:
        # Write REJECT_FINAL as normal
        ...

Level 2 five-component pipeline (all synchronous, in-process):
  1. Signal Collector   — reads 6 runtime signals from in-process context
  2. Baseline Comparator — computes deviation ratios vs pre-warmed baselines
  3. Trust Scorer       — weighted 0–100 score across 6 signals
  4. Policy Evaluator   — per-rail/per-client threshold check
  5. Decision Emitter   — returns TrustDecision; publishes async quarantine event

Constraints per architecture spec:
  · < 1ms total decision latency
  · < 2MB memory footprint per service instance
  · Zero external I/O in the reject path
  · Quarantine event is published asynchronously — never blocks the payment thread
"""
from __future__ import annotations

import time
from typing import Callable

from .models import RejectCandidate, TrustDecision, TrustAction, RejectClass
from .baseline import BaselineCache, BaselineComparator
from .signals import SignalCollector
from .scorer import TrustScorer
from .policy import PolicyEvaluator, RailPolicy, ClientPolicy


class SRQSDK:
    """
    The SRQ embedded library.
    One instance per service process; shared across threads (stateless evaluate path).
    """

    def __init__(
        self,
        baseline_cache:        BaselineCache       | None = None,
        policy_evaluator:      PolicyEvaluator     | None = None,
        quarantine_publisher:  Callable[[RejectCandidate, TrustDecision], None] | None = None,
        audit_writer:          Callable[[dict], None] | None = None,
        shadow_mode:           bool = False,
    ):
        """
        Parameters
        ----------
        baseline_cache       Pre-warmed baseline store (defaults to in-memory).
        policy_evaluator     Rail/client policy store (defaults to conservative defaults).
        quarantine_publisher Async callback for quarantine events → event bus.
                             Must not block; called after decision is returned.
        audit_writer         Inline audit callback; receives one dict per evaluate() call.
        shadow_mode          Phase 1 behaviour: classify but never quarantine.
        """
        cache = baseline_cache or BaselineCache()
        comparator = BaselineComparator(cache)

        self._collector  = SignalCollector(comparator)
        self._scorer     = TrustScorer()
        self._policy     = policy_evaluator or PolicyEvaluator()
        self._publish    = quarantine_publisher
        self._audit      = audit_writer
        self.shadow_mode = shadow_mode

    # ── Public API ──────────────────────────────────────────────────────────

    def evaluate(self, candidate: RejectCandidate) -> TrustDecision:
        """
        Core entry point — call at the reject decision point.

        Returns TrustDecision synchronously in < 1ms.
        If action == QUARANTINE:  set external payment state to PROCESSING_DELAYED.
        If action == PASS:        proceed to REJECT_FINAL as normal.
        """
        t0 = time.perf_counter()

        # ── Component 1: Signal Collector ───────────────────────────────────
        readout = self._collector.collect(candidate)

        # ── Components 2 + 3: Baseline Comparator + Trust Scorer ────────────
        trust_score, signals_fired = self._scorer.score(readout, candidate.rail.value)

        # ── Component 3b: Classification ────────────────────────────────────
        classification, confidence = TrustScorer.classify(trust_score)

        # ── Component 4: Policy Evaluator ───────────────────────────────────
        action, reason = self._policy.evaluate(
            trust_score, classification, confidence, candidate.rail.value
        )

        # Phase 1 (shadow mode): score every reject but never quarantine
        if self.shadow_mode and action == TrustAction.QUARANTINE:
            action = TrustAction.PASS
            reason = f"[SHADOW] would quarantine — {reason}"

        # ── Component 5: Decision Emitter ────────────────────────────────────
        decision = TrustDecision(
            payment_id     = candidate.payment_id,
            action         = action,
            trust_score    = trust_score,
            classification = classification,
            confidence     = confidence,
            signals_fired  = signals_fired,
            reason         = reason,
        )

        # Async quarantine event — published after decision is assembled,
        # never blocks the caller.
        if action == TrustAction.QUARANTINE and self._publish:
            self._publish(candidate, decision)

        # Inline audit write (synchronous but lightweight dict append)
        elapsed_us = round((time.perf_counter() - t0) * 1_000_000)
        if self._audit:
            self._audit({
                "payment_id":    candidate.payment_id,
                "rail":          candidate.rail.value,
                "code":          candidate.reject_reason_code,
                "trust_score":   trust_score,
                "action":        action.value,
                "classification": classification.value,
                "confidence":    round(confidence, 3),
                "signals_fired": signals_fired,
                "shadow_mode":   self.shadow_mode,
                "latency_us":    elapsed_us,
            })

        return decision

    def trust_score(self, candidate: RejectCandidate) -> int:
        """Return the raw trust score (0–100) without publishing any events."""
        readout = self._collector.collect(candidate)
        score, _ = self._scorer.score(readout, candidate.rail.value)
        return score

    def quarantine_or_pass(self, candidate: RejectCandidate) -> TrustAction:
        """Convenience wrapper — return just the action."""
        return self.evaluate(candidate).action

    # ── Configuration (can be hot-reloaded from PolicyGovernor) ─────────────

    def update_rail_policy(self, policy: RailPolicy) -> None:
        self._policy.update_rail(policy)

    def update_client_policy(self, policy: ClientPolicy) -> None:
        self._policy.update_client(policy)

    def set_shadow_mode(self, enabled: bool) -> None:
        self.shadow_mode = enabled
