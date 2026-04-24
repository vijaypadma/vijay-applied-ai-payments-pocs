"""
SRQ Control Plane — Level 3.

Async, out-of-band.  Never in the payment thread.
Receives quarantine events from the SDK, groups them by root cause,
monitors recovery, runs staged replay, and seals the audit trail.

Six components (per architecture spec):
  1. QuarantineIngestor — consumes SDK events; creates QuarantineRecords
  2. AnomalyGrouper     — clusters records by root cause (signal-space proximity)
  3. RecoveryDetector   — polls 4 recovery signals; 3-of-4 × 2 cycles before replay
  4. CanaryEngine       — 10% cohort first; ≥ 95% success before full ramp
  5. PolicyGovernor     — per-rail/per-client rules; replay-window registry
  6. AuditStore         — immutable causal chain; compliance-ready

State contract (external vs internal):
  External clients always see PROCESSING_DELAYED — never QUARANTINED.
  Internal states (QUARANTINED → AWAITING_RESOLUTION → REPLAY_ELIGIBLE →
  REPLAYED → CLEARED | LEGITIMATELY_REJECTED) are control-plane-only.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

from .models import (
    RejectCandidate, TrustDecision, QuarantineRecord,
    AnomalyGroup, QueueState, Rail,
)
from .policy import PolicyGovernor


# ============================================================================
# 6. Audit Store
# ============================================================================

class AuditStore:
    """
    Append-only log of every trust decision, quarantine event, and replay action.
    Production implementation: PostgreSQL / Cassandra append-only tables.
    Every entry carries a full signal snapshot — the immutable causal chain
    regulators and bank examiners require.
    """

    def __init__(self):
        self._entries: list[dict] = []

    def write(self, entry: dict) -> None:
        entry["stored_at_us"] = int(time.time() * 1_000_000)
        self._entries.append(entry)

    def query(
        self,
        payment_id:  str | None = None,
        anomaly_id:  str | None = None,
        event_type:  str | None = None,
    ) -> list[dict]:
        rows = self._entries
        if payment_id:
            rows = [e for e in rows if e.get("payment_id") == payment_id]
        if anomaly_id:
            rows = [e for e in rows if e.get("anomaly_id") == anomaly_id]
        if event_type:
            rows = [e for e in rows if e.get("event") == event_type]
        return rows

    @property
    def count(self) -> int:
        return len(self._entries)


# ============================================================================
# 1. Quarantine Ingestor
# ============================================================================

class QuarantineIngestor:
    """
    Consumes SDK quarantine events from the event bus.
    Creates QuarantineRecords in state QUARANTINED.
    External state contract: downstream always sees PROCESSING_DELAYED.
    """

    def __init__(self, audit: AuditStore):
        self._audit   = audit
        self._records: dict[str, QuarantineRecord] = {}   # record_id → record

    def ingest(
        self, candidate: RejectCandidate, decision: TrustDecision
    ) -> QuarantineRecord:
        rail = candidate.rail
        expires_us = (
            int(time.time() * 1_000_000) + rail.max_quarantine_seconds * 1_000_000
        )
        record = QuarantineRecord(
            candidate      = candidate,
            decision       = decision,
            state          = QueueState.QUARANTINED,
            expires_at_us  = expires_us,
        )
        self._records[record.record_id] = record

        self._audit.write({
            "event":          "QUARANTINED",
            "payment_id":     candidate.payment_id,
            "record_id":      record.record_id,
            "rail":           rail.value,
            "reject_code":    candidate.reject_reason_code,
            "trust_score":    decision.trust_score,
            "signals_fired":  decision.signals_fired,
            "external_state": "PROCESSING_DELAYED",
        })
        return record

    def get(self, record_id: str) -> QuarantineRecord | None:
        return self._records.get(record_id)

    def all_records(self) -> list[QuarantineRecord]:
        return list(self._records.values())

    def active_records(self) -> list[QuarantineRecord]:
        return [
            r for r in self._records.values()
            if r.state not in (QueueState.CLEARED, QueueState.LEGITIMATELY_REJECTED)
        ]

    def expired_records(self) -> list[QuarantineRecord]:
        return [r for r in self.active_records() if r.is_expired]


# ============================================================================
# 2. Anomaly Grouper
# ============================================================================

class AnomalyGrouper:
    """
    Clusters QuarantineRecords by root cause.

    Production implementation: DBSCAN over the 6-dimensional signal vector.
    Here: simplified fingerprint — dominant signal + reject_code within a
    5-minute cohort window.  Good enough for the pilot rail.
    """

    COHORT_WINDOW_US = 5 * 60 * 1_000_000   # 5-minute cohort window

    def __init__(self, audit: AuditStore):
        self._audit  = audit
        self._groups: dict[str, AnomalyGroup]    = {}   # anomaly_id → group
        self._rec_to_group: dict[str, str]        = {}   # record_id  → anomaly_id

    def _fingerprint(self, record: QuarantineRecord) -> str:
        """Simplified root-cause fingerprint (dominant fired signal + code)."""
        signals = record.decision.signals_fired if record.decision else []
        dominant = signals[0].split("=")[0] if signals else "unknown"
        return f"{dominant}__{record.candidate.reject_reason_code}"

    def assign(self, record: QuarantineRecord) -> AnomalyGroup:
        """Assign a record to an existing or new anomaly group."""
        fp  = self._fingerprint(record)
        now = int(time.time() * 1_000_000)

        # Look for an open group with the same fingerprint in the cohort window
        for group in self._groups.values():
            if (
                group.root_cause == fp
                and not group.is_resolved
                and (now - group.detected_at_us) < self.COHORT_WINDOW_US
            ):
                group.record_ids.append(record.record_id)
                self._rec_to_group[record.record_id] = group.anomaly_id
                record.anomaly_id = group.anomaly_id
                return group

        # New anomaly group
        group = AnomalyGroup(root_cause=fp, record_ids=[record.record_id])
        self._groups[group.anomaly_id] = group
        self._rec_to_group[record.record_id] = group.anomaly_id
        record.anomaly_id = group.anomaly_id

        self._audit.write({
            "event":          "ANOMALY_DETECTED",
            "anomaly_id":     group.anomaly_id,
            "root_cause":     fp,
            "first_record_id": record.record_id,
        })
        return group

    def get_group(self, anomaly_id: str) -> AnomalyGroup | None:
        return self._groups.get(anomaly_id)

    def open_groups(self) -> list[AnomalyGroup]:
        return [g for g in self._groups.values() if not g.is_resolved]

    def resolve(self, anomaly_id: str) -> None:
        g = self._groups.get(anomaly_id)
        if g:
            g.resolved_at_us = int(time.time() * 1_000_000)
            self._audit.write({"event": "ANOMALY_RESOLVED", "anomaly_id": anomaly_id})


# ============================================================================
# 3. Recovery Detector
# ============================================================================

@dataclass
class RecoveryStatus:
    dependency_healthy: bool = False
    reject_rate_normal: bool = False
    cache_fresh:        bool = False
    latency_normal:     bool = False

    def confirmed_count(self) -> int:
        return sum([
            self.dependency_healthy,
            self.reject_rate_normal,
            self.cache_fresh,
            self.latency_normal,
        ])


class RecoveryDetector:
    """
    Polls 4 recovery signals per anomaly group.
    Recovery is authorised only when 3-of-4 signals confirmed
    for 2 consecutive polling cycles.

    Signal callables return True = healthy / recovered.
    In production these call health-check endpoints or read
    from the same baseline cache that feeds the SDK.
    """

    REQUIRED_CONFIRMED = 3
    REQUIRED_CYCLES    = 2

    def __init__(
        self,
        audit:              AuditStore,
        dependency_health:  Callable[[], bool] | None = None,
        reject_rate_ok:     Callable[[], bool] | None = None,
        cache_fresh:        Callable[[], bool] | None = None,
        latency_ok:         Callable[[], bool] | None = None,
    ):
        self._audit    = audit
        self._dep      = dependency_health or (lambda: True)
        self._rate     = reject_rate_ok    or (lambda: True)
        self._cache    = cache_fresh       or (lambda: True)
        self._latency  = latency_ok        or (lambda: True)
        self._cycles:  dict[str, int] = defaultdict(int)   # anomaly_id → consecutive ok cycles

    def poll(self, anomaly_id: str) -> tuple[bool, RecoveryStatus]:
        """
        Returns (recovery_authorised, RecoveryStatus).
        Must be called from the background poller — never from the payment thread.
        """
        status = RecoveryStatus(
            dependency_healthy = self._dep(),
            reject_rate_normal = self._rate(),
            cache_fresh        = self._cache(),
            latency_normal     = self._latency(),
        )
        confirmed = status.confirmed_count()

        if confirmed >= self.REQUIRED_CONFIRMED:
            self._cycles[anomaly_id] += 1
        else:
            self._cycles[anomaly_id] = 0   # reset on any regression

        cycles     = self._cycles[anomaly_id]
        authorised = cycles >= self.REQUIRED_CYCLES

        self._audit.write({
            "event":              "RECOVERY_POLL",
            "anomaly_id":         anomaly_id,
            "confirmed_signals":  confirmed,
            "consecutive_cycles": cycles,
            "authorised":         authorised,
            "status": {
                "dep":     status.dependency_healthy,
                "rate":    status.reject_rate_normal,
                "cache":   status.cache_fresh,
                "latency": status.latency_normal,
            },
        })
        return authorised, status


# ============================================================================
# 4. Canary Engine
# ============================================================================

class CanaryEngine:
    """
    Staged replay — prevents thundering herd on a freshly-recovered system.

    Phase 1 (canary): replay CANARY_PCT (10%) of the cohort.
      · Sort by rail urgency then descending amount.
      · If ≥ 95% clear → proceed to full ramp.
      · If < 95% clear → hold; do NOT ramp. Requeue for next recovery cycle.

    Phase 2 (full ramp): replay all remaining QUARANTINED records.
    """

    CANARY_PCT         = 0.10
    SUCCESS_THRESHOLD  = 0.95

    def __init__(
        self,
        audit:     AuditStore,
        replay_fn: Callable[[RejectCandidate], bool],   # True = payment cleared
    ):
        self._audit   = audit
        self._replay  = replay_fn

    def _sorted_by_priority(
        self, records: list[QuarantineRecord]
    ) -> list[QuarantineRecord]:
        return sorted(
            records,
            key=lambda r: (r.candidate.rail.priority, -r.candidate.amount_cents),
        )

    def run_canary(
        self, anomaly_id: str, records: list[QuarantineRecord]
    ) -> tuple[bool, dict]:
        """
        Replay CANARY_PCT of records.
        Returns (canary_passed, stats_dict).
        """
        n_canary = max(1, round(len(records) * self.CANARY_PCT))
        batch    = self._sorted_by_priority(records)[:n_canary]

        cleared = legitimately_rejected = 0

        for rec in batch:
            rec.transition(QueueState.REPLAY_ELIGIBLE, "selected for canary batch")
            cleared_flag = self._replay(rec.candidate)
            rec.replay_attempts += 1

            if cleared_flag:
                rec.transition(QueueState.CLEARED, "canary replay: payment cleared")
                cleared += 1
            else:
                rec.transition(
                    QueueState.LEGITIMATELY_REJECTED,
                    "canary replay: confirmed legitimate reject",
                )
                legitimately_rejected += 1

            rec.transition(QueueState.REPLAYED, "canary replay complete")

        success_rate = cleared / len(batch) if batch else 1.0
        passed       = success_rate >= self.SUCCESS_THRESHOLD

        stats = {
            "canary_size":            n_canary,
            "cleared":                cleared,
            "legitimately_rejected":  legitimately_rejected,
            "success_rate":           round(success_rate, 4),
            "passed":                 passed,
        }
        self._audit.write({"event": "CANARY_COMPLETE", "anomaly_id": anomaly_id, **stats})
        return passed, stats

    def run_full_ramp(
        self, anomaly_id: str, records: list[QuarantineRecord]
    ) -> dict:
        """
        Replay all remaining QUARANTINED records after a successful canary.
        """
        remaining = [
            r for r in records
            if r.state not in (
                QueueState.CLEARED,
                QueueState.LEGITIMATELY_REJECTED,
                QueueState.REPLAYED,
            )
        ]
        cleared = legitimately_rejected = 0

        for rec in self._sorted_by_priority(remaining):
            rec.transition(QueueState.REPLAY_ELIGIBLE, "full ramp")
            cleared_flag = self._replay(rec.candidate)
            rec.replay_attempts += 1

            if cleared_flag:
                rec.transition(QueueState.CLEARED, "full ramp: payment cleared")
                cleared += 1
            else:
                rec.transition(
                    QueueState.LEGITIMATELY_REJECTED,
                    "full ramp: confirmed legitimate reject",
                )
                legitimately_rejected += 1

            rec.transition(QueueState.REPLAYED, "full ramp complete")

        stats = {
            "ramp_size":              len(remaining),
            "cleared":                cleared,
            "legitimately_rejected":  legitimately_rejected,
        }
        self._audit.write({"event": "FULL_RAMP_COMPLETE", "anomaly_id": anomaly_id, **stats})
        return stats


# ============================================================================
# Control Plane Orchestrator
# ============================================================================

class SRQControlPlane:
    """
    Top-level orchestrator for all Level 3 components.

    Called by the event bus consumer (Kafka / Kinesis consumer group).
    Never invoked from the payment thread.

    Wiring:
      SDK  → quarantine_publisher → on_quarantine_event()
      Poller → run_recovery_cycle()  (called on a timer, e.g. every 30s)
    """

    def __init__(
        self,
        replay_fn:          Callable[[RejectCandidate], bool],
        dependency_health:  Callable[[], bool] | None = None,
        reject_rate_ok:     Callable[[], bool] | None = None,
        cache_fresh:        Callable[[], bool] | None = None,
        latency_ok:         Callable[[], bool] | None = None,
    ):
        self.audit     = AuditStore()
        self.ingestor  = QuarantineIngestor(self.audit)
        self.grouper   = AnomalyGrouper(self.audit)
        self.recovery  = RecoveryDetector(
            self.audit,
            dependency_health = dependency_health,
            reject_rate_ok    = reject_rate_ok,
            cache_fresh       = cache_fresh,
            latency_ok        = latency_ok,
        )
        self.canary    = CanaryEngine(self.audit, replay_fn)
        self.governor  = PolicyGovernor()

    # ── Ingest path (called by event bus consumer) ────────────────────────

    def on_quarantine_event(
        self, candidate: RejectCandidate, decision: TrustDecision
    ) -> QuarantineRecord:
        """
        Entry point for quarantine events from the SDK.
        Creates a QuarantineRecord, assigns it to an anomaly group.
        """
        record = self.ingestor.ingest(candidate, decision)
        record.transition(QueueState.AWAITING_RESOLUTION, "assigned to control plane")
        self.grouper.assign(record)
        return record

    # ── Recovery cycle (called by background poller) ──────────────────────

    def run_recovery_cycle(self) -> list[dict]:
        """
        Poll recovery for every open anomaly group.
        When recovery is authorised: run canary → full ramp.
        Returns a list of action dicts (one per group processed).
        """
        actions: list[dict] = []

        for group in self.grouper.open_groups():
            authorised, status = self.recovery.poll(group.anomaly_id)

            if not authorised:
                actions.append({
                    "anomaly_id":        group.anomaly_id,
                    "action":            "waiting_for_recovery",
                    "signals_confirmed": status.confirmed_count(),
                    "cohort_size":       len(group.record_ids),
                })
                continue

            # Collect records still awaiting resolution
            records = [
                r
                for rid in group.record_ids
                if (r := self.ingestor.get(rid))
                and r.state == QueueState.AWAITING_RESOLUTION
            ]

            if not records:
                self.grouper.resolve(group.anomaly_id)
                continue

            # Reset to QUARANTINED so the canary machinery can act on them
            for r in records:
                r.state = QueueState.QUARANTINED

            canary_passed, canary_stats = self.canary.run_canary(
                group.anomaly_id, records
            )

            if canary_passed:
                ramp_stats = self.canary.run_full_ramp(group.anomaly_id, records)
                self.grouper.resolve(group.anomaly_id)
                actions.append({
                    "anomaly_id":  group.anomaly_id,
                    "action":      "full_ramp_complete",
                    "root_cause":  group.root_cause,
                    "canary":      canary_stats,
                    "ramp":        ramp_stats,
                })
            else:
                # Canary failed — system may still be degraded or rejects are legitimate
                # Records stay QUARANTINED for the next recovery cycle
                for r in records:
                    if r.state == QueueState.QUARANTINED:
                        r.transition(
                            QueueState.AWAITING_RESOLUTION,
                            "canary failed — re-queued for next recovery cycle",
                        )
                actions.append({
                    "anomaly_id": group.anomaly_id,
                    "action":     "canary_failed_requeued",
                    "canary":     canary_stats,
                })

        # Escalate expired records to manual review
        for rec in self.ingestor.expired_records():
            rec.transition(
                QueueState.AWAITING_RESOLUTION,
                "TTL expired — escalated to manual review",
            )
            self.audit.write({
                "event":      "TTL_ESCALATION",
                "record_id":  rec.record_id,
                "payment_id": rec.candidate.payment_id,
                "rail":       rec.candidate.rail.value,
            })

        return actions

    # ── Dashboard ─────────────────────────────────────────────────────────

    def dashboard(self) -> dict:
        records   = self.ingestor.all_records()
        by_state: dict[str, int] = defaultdict(int)
        for r in records:
            by_state[r.state.value] += 1
        return {
            "total_quarantined":   len(records),
            "open_anomaly_groups": len(self.grouper.open_groups()),
            "audit_entries":       self.audit.count,
            "by_state":            dict(by_state),
        }
