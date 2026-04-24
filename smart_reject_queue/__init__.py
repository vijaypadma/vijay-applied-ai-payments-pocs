"""
meridian_smart_queue — Smart Reject Queue (SRQ)

A trust layer for payment systems that intercepts every reject before it
becomes terminal state, distinguishes infra-induced false rejects from
legitimate business rejections, and quietly recovers the false ones.

Quick start:
    from meridian_smart_queue import SRQSDK, SRQControlPlane, TrustAction

    cp  = SRQControlPlane(replay_fn=my_payment_pipeline.process)
    sdk = SRQSDK(quarantine_publisher=cp.on_quarantine_event)

    decision = sdk.evaluate(candidate)
    if decision.action == TrustAction.QUARANTINE:
        set_external_state(candidate.payment_id, "PROCESSING_DELAYED")
    else:
        write_reject_final(candidate.payment_id)
"""

from .models import (
    Rail, RejectClass, TrustAction, QueueState,
    InfraSignals, RejectCandidate, TrustDecision,
    QuarantineRecord, AnomalyGroup,
)
from .baseline import BaselineCache, BaselineComparator, BaselineEntry
from .signals  import SignalCollector, SignalReadout
from .scorer   import TrustScorer
from .policy   import PolicyEvaluator, PolicyGovernor, RailPolicy, ClientPolicy, ReplayWindow
from .sdk      import SRQSDK
from .control_plane import (
    SRQControlPlane, AuditStore,
    QuarantineIngestor, AnomalyGrouper,
    RecoveryDetector, RecoveryStatus, CanaryEngine,
)

__all__ = [
    # models
    "Rail", "RejectClass", "TrustAction", "QueueState",
    "InfraSignals", "RejectCandidate", "TrustDecision",
    "QuarantineRecord", "AnomalyGroup",
    # baseline
    "BaselineCache", "BaselineComparator", "BaselineEntry",
    # signals
    "SignalCollector", "SignalReadout",
    # scorer
    "TrustScorer",
    # policy
    "PolicyEvaluator", "PolicyGovernor", "RailPolicy", "ClientPolicy", "ReplayWindow",
    # sdk
    "SRQSDK",
    # control plane
    "SRQControlPlane", "AuditStore",
    "QuarantineIngestor", "AnomalyGrouper",
    "RecoveryDetector", "RecoveryStatus", "CanaryEngine",
]
