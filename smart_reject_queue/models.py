"""
SRQ Models — enums, dataclasses, state machine.
Every quarantine record carries the full causal chain for audit and replay.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Rail
# ---------------------------------------------------------------------------

class Rail(str, Enum):
    ACH   = "ACH"
    WIRE  = "WIRE"
    RTP   = "RTP"
    ZELLE = "ZELLE"
    FX    = "FX"

    @property
    def max_quarantine_seconds(self) -> int:
        """Maximum hold time before escalation to manual review."""
        return {
            Rail.RTP:   300,    # 5 min  — real-time rail
            Rail.ZELLE: 300,    # 5 min  — real-time rail
            Rail.WIRE:  3_600,  # 1 hr   — same-day finality
            Rail.FX:    1_800,  # 30 min — rate-lock window
            Rail.ACH:   86_400, # 24 hr  — batch processing window
        }[self]

    @property
    def priority(self) -> int:
        """Lower = higher priority in canary replay queue."""
        return {Rail.RTP: 1, Rail.ZELLE: 1, Rail.WIRE: 2, Rail.FX: 3, Rail.ACH: 4}[self]


# ---------------------------------------------------------------------------
# Classification & Action
# ---------------------------------------------------------------------------

class RejectClass(str, Enum):
    LEGITIMATE = "LEGITIMATE"   # real business rejection — let it through
    SUSPECT    = "SUSPECT"      # ambiguous — hold for review
    SYSTEMIC   = "SYSTEMIC"     # infra-induced — false reject


class TrustAction(str, Enum):
    PASS       = "PASS"         # write REJECT_FINAL, notify client
    QUARANTINE = "QUARANTINE"   # set external state to PROCESSING_DELAYED


# ---------------------------------------------------------------------------
# Queue State Machine
# REJECTED → QUARANTINED → AWAITING_RESOLUTION → REPLAY_ELIGIBLE
#          → REPLAYED → { CLEARED | LEGITIMATELY_REJECTED }
# ---------------------------------------------------------------------------

class QueueState(str, Enum):
    REJECTED              = "REJECTED"
    QUARANTINED           = "QUARANTINED"
    AWAITING_RESOLUTION   = "AWAITING_RESOLUTION"
    REPLAY_ELIGIBLE       = "REPLAY_ELIGIBLE"
    REPLAYED              = "REPLAYED"
    CLEARED               = "CLEARED"
    LEGITIMATELY_REJECTED = "LEGITIMATELY_REJECTED"


# ---------------------------------------------------------------------------
# The 6 Infrastructure Signals
# ---------------------------------------------------------------------------

@dataclass
class InfraSignals:
    """
    Six runtime signals read at reject time.
    All values sourced in-process from local cache client — zero external I/O.
    """
    reject_burst_ratio: float       # current reject rate / p95 baseline  (1.0 = normal)
    latency_deviation_ms: float     # ms above p99 processing latency     (0 = at/below p99)
    upstream_5xx_rate: float        # fraction of upstream calls → 5xx    (0–1)
    cache_miss_rate: float          # fraction of cache reads that missed  (0–1)
    dependency_health_score: float  # composite connector health           (1.0 = fully healthy)
    cert_error_rate: float          # fraction of calls with cert/auth err (0–1)


# ---------------------------------------------------------------------------
# Reject Candidate (input to SDK)
# ---------------------------------------------------------------------------

@dataclass
class RejectCandidate:
    """Everything the SDK needs to evaluate a single reject decision."""
    payment_id:          str
    idempotency_key:     str
    amount_cents:        int
    currency:            str
    rail:                Rail
    originator_id:       str
    beneficiary_id:      str
    reject_reason_code:  str
    signals:             InfraSignals
    processing_latency_ms: float
    timestamp_us:        int = field(default_factory=lambda: int(time.time() * 1_000_000))

    @classmethod
    def create(
        cls,
        amount_cents: int,
        currency: str,
        rail: Rail,
        originator_id: str,
        beneficiary_id: str,
        reject_reason_code: str,
        signals: InfraSignals,
        processing_latency_ms: float,
    ) -> "RejectCandidate":
        return cls(
            payment_id=str(uuid.uuid4()),
            idempotency_key=str(uuid.uuid4()),
            amount_cents=amount_cents,
            currency=currency,
            rail=rail,
            originator_id=originator_id,
            beneficiary_id=beneficiary_id,
            reject_reason_code=reject_reason_code,
            signals=signals,
            processing_latency_ms=processing_latency_ms,
        )


# ---------------------------------------------------------------------------
# Trust Decision (output of SDK five-component pipeline)
# ---------------------------------------------------------------------------

@dataclass
class TrustDecision:
    """
    Returned synchronously by sdk.evaluate().
    action == QUARANTINE → set external state to PROCESSING_DELAYED.
    action == PASS       → write REJECT_FINAL normally.
    """
    payment_id:     str
    action:         TrustAction
    trust_score:    int           # 0–100; higher = more legitimate
    classification: RejectClass
    confidence:     float         # 0.0–1.0
    signals_fired:  list[str]     # human-readable signal evidence
    reason:         str
    timestamp_us:   int = field(default_factory=lambda: int(time.time() * 1_000_000))


# ---------------------------------------------------------------------------
# Control Plane Records
# ---------------------------------------------------------------------------

@dataclass
class QuarantineRecord:
    """
    Internal control-plane record.
    External clients only ever see PROCESSING_DELAYED — never QUARANTINED.
    """
    record_id:        str           = field(default_factory=lambda: str(uuid.uuid4()))
    candidate:        Optional[RejectCandidate]  = None
    decision:         Optional[TrustDecision]    = None
    state:            QueueState    = QueueState.QUARANTINED
    anomaly_id:       Optional[str] = None
    quarantined_at_us: int          = field(default_factory=lambda: int(time.time() * 1_000_000))
    expires_at_us:    Optional[int] = None
    replay_attempts:  int           = 0
    audit_trail:      list[dict]    = field(default_factory=list)

    def transition(self, new_state: QueueState, reason: str = "") -> None:
        old = self.state
        self.state = new_state
        self.audit_trail.append({
            "ts_us":   int(time.time() * 1_000_000),
            "from":    old.value,
            "to":      new_state.value,
            "reason":  reason,
        })

    @property
    def is_expired(self) -> bool:
        if self.expires_at_us is None:
            return False
        return int(time.time() * 1_000_000) > self.expires_at_us


@dataclass
class AnomalyGroup:
    """A cluster of QuarantineRecords sharing the same inferred root cause."""
    anomaly_id:           str       = field(default_factory=lambda: str(uuid.uuid4()))
    root_cause:           str       = ""
    record_ids:           list[str] = field(default_factory=list)
    detected_at_us:       int       = field(default_factory=lambda: int(time.time() * 1_000_000))
    resolved_at_us:       Optional[int] = None
    consecutive_cycles:   int       = 0   # recovery cycles confirmed so far

    @property
    def is_resolved(self) -> bool:
        return self.resolved_at_us is not None
