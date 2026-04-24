# Smart Reject Queue (SRQ)

**What if your platform stopped trusting rejects it cannot explain?**

SRQ is a trust layer for payment systems that intercepts every reject before it becomes terminal state. It evaluates six infrastructure signals in under 1ms and asks one question: *can this reject be trusted?* Legitimate rejects pass through immediately. Suspect rejects are quarantined and recovered quietly.

---

## The Problem

Not every reject is a real reject.

Infrastructure fails constantly — services time out, caches go stale, certificates rotate, network partitions flap. Each failure produces a reject signal that looks identical to a legitimate business decline. Your platform cannot tell the difference.

So it writes `REJECT_FINAL`, publishes the event, sends the client notification, and hands the exception to operations. The cleanup costs more than the failure itself.

| Metric | Impact |
|---|---|
| **$20–30B** | Annual economic stake from illegitimate reject noise globally |
| **25–40%** | Of all rejects are infra-induced — not business decisions |
| **4–8 hrs** | Avg ops recovery per major reject wave incident |
| **STP −12%** | Processing loss during peak degradation windows |

---

## Architecture — Four Levels

```
┌─────────────────────────────────────────────────────────────────────┐
│  LEVEL 1 · PAYMENT MICROSERVICES + SRQ SDK (embedded library)       │
│                                                                     │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐  │
│  │ Payment          │  │ Account / DDA    │  │ Clearing         │  │
│  │ Orchestrator     │  │ Service          │  │ Gateway          │  │
│  │                  │  │                  │  │                  │  │
│  │  ┌────────────┐  │  │  ┌────────────┐  │  │  ┌────────────┐  │  │
│  │  │  SRQ SDK   │  │  │  │  SRQ SDK   │  │  │  │  SRQ SDK   │  │  │
│  │  │ evaluate() │  │  │  │ evaluate() │  │  │  │ evaluate() │  │  │
│  │  └────────────┘  │  │  └────────────┘  │  │  └────────────┘  │  │
│  └──────────────────┘  └──────────────────┘  └──────────────────┘  │
│     No sidecar · No proxy · No network call · < 1ms · < 2MB        │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  LEVEL 2 · SDK INTERNALS — Five-component pipeline, synchronous     │
│                                                                     │
│  Signal      ► Baseline     ► Trust       ► Policy      ► Decision  │
│  Collector     Comparator     Scorer        Evaluator     Emitter   │
│  (6 signals)   (burst ratio)  (0–100)       (thresholds)  (action)  │
│                                                                     │
│  ■ QUARANTINE → async event to control plane (never blocks thread)  │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  LEVEL 3 · CONTROL PLANE — Async, out-of-band                      │
│                                                                     │
│  Quarantine  ► Anomaly   ► Recovery   ► Canary    ► Policy  ► Audit │
│  Ingestor      Grouper     Detector     Engine      Governor  Store │
│  (records)     (DBSCAN)    (3/4 × 2)    (10%→95%)  (rules)   (log) │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  LEVEL 4 · SHARED INFRASTRUCTURE                                    │
│                                                                     │
│  Baseline Cache          Event Bus             Audit Store          │
│  Redis / GemFire         Kafka / Kinesis        PostgreSQL          │
│  Pre-warmed at deploy    Quarantine events      Immutable log       │
│  All reads < 0.1ms       PROCESSING_DELAYED     Full causal chain   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Package Layout

```
meridian_smart_queue/
├── __init__.py          # Public API exports
├── models.py            # Enums, dataclasses, state machine
├── baseline.py          # Level 4: BaselineCache + BaselineComparator
├── signals.py           # Level 2-1: SignalCollector (6 runtime signals)
├── scorer.py            # Level 2-3: TrustScorer (weighted 0–100)
├── policy.py            # Level 2-4 + 3-5: PolicyEvaluator + PolicyGovernor
├── sdk.py               # Level 1+2: SRQSDK (five-component pipeline)
├── control_plane.py     # Level 3: Ingestor, Grouper, Recovery, Canary, Audit
└── demo.py              # Five-phase delivery simulation
```

---

## Quick Start

### Run the demo

```bash
python -m meridian_smart_queue.demo
```

This runs all five delivery phases:

| Phase | Name | Behaviour |
|---|---|---|
| 01 | **Shadow** | Observe only. Score every reject. Zero enforcement. Baseline builds. |
| 02 | **Classify** | Dashboard live. LEGITIMATE / SUSPECT / SYSTEMIC classification. No state change. |
| 03 | **Quarantine** | Pilot rail (ACH) active. Suspect rejects held as `PROCESSING_DELAYED`. |
| 04 | **Replay** | Recovery confirmed (3-of-4 signals × 2 cycles). Canary 10% → full ramp. |
| 05 | **Platform** | All rails, all clients. Multi-tenant policy. Summary stats. |

### Embed in a payment service

```python
from meridian_smart_queue import SRQSDK, SRQControlPlane, TrustAction

# Level 3 — control plane (runs out-of-band)
control_plane = SRQControlPlane(
    replay_fn=my_payment_pipeline.reprocess,
    dependency_health=lambda: health_check("partner-bank"),
    reject_rate_ok=lambda: get_reject_rate("ACH") < baseline_p95,
    cache_fresh=lambda: redis_client.ttl("account-cache") > 0,
    latency_ok=lambda: get_p99_latency() < 300,
)

# Level 1+2 — SDK (embedded in your service)
sdk = SRQSDK(
    quarantine_publisher=control_plane.on_quarantine_event,
    shadow_mode=False,
)

# At the reject decision point:
decision = sdk.evaluate(candidate)

if decision.action == TrustAction.QUARANTINE:
    # Do NOT write REJECT_FINAL
    set_external_state(candidate.payment_id, "PROCESSING_DELAYED")
else:
    # Legitimate reject — proceed normally
    write_reject_final(candidate.payment_id)
```

---

## The 6 Infrastructure Signals

Every reject is evaluated against six runtime signals, all read from in-process context (zero external I/O):

| # | Signal | What it measures | Systemic indicator |
|---|---|---|---|
| 1 | **Reject burst ratio** | Current reject rate / p95 baseline | 3× baseline = cache eviction wave |
| 2 | **Latency deviation** | Processing latency / p99 baseline | 2× p99 = upstream timeout cascade |
| 3 | **Upstream 5xx rate** | Fraction of upstream calls returning 5xx | >10% = partner bank outage |
| 4 | **Cache miss rate** | Fraction of cache reads that missed | >20% above baseline = stale data |
| 5 | **Dependency health** | Composite health score of connectors | <50% = service degradation |
| 6 | **Cert/auth error rate** | Fraction of calls with cert errors | >5% = certificate rotation issue |

These signals are weighted per-rail (RTP weights latency higher, ACH weights burst ratio higher) and combined into a single **trust score 0–100**.

---

## Trust Score → Classification

```
  100 ┌─────────────────────────┐
      │      LEGITIMATE         │  Trust score ≥ 75
      │  Reject is trustworthy  │  → PASS (write REJECT_FINAL)
   75 ├─────────────────────────┤
      │       SUSPECT           │  40 ≤ trust < 75
      │  Ambiguous — hold it    │  → QUARANTINE if confidence ≥ threshold
   40 ├─────────────────────────┤
      │       SYSTEMIC          │  Trust score < 40
      │  Infra-induced reject   │  → QUARANTINE
    0 └─────────────────────────┘
```

---

## State Machine

Quarantined payments follow this lifecycle:

```
REJECTED → QUARANTINED → AWAITING_RESOLUTION → REPLAY_ELIGIBLE
                                                      │
                                                      ▼
                                                   REPLAYED
                                                   /      \
                                                  ▼        ▼
                                              CLEARED   LEGITIMATELY_REJECTED
```

External clients only see `PROCESSING_DELAYED` — never `QUARANTINED`. This is the state contract.

---

## Recovery & Replay

Recovery is authorised conservatively:

1. **4 recovery signals** are polled: dependency health, reject rate normalisation, cache freshness, latency normalisation
2. **3 of 4** signals must confirm healthy
3. Confirmation must hold for **2 consecutive polling cycles**
4. **Canary replay**: 10% of the quarantined cohort is replayed first
5. **≥ 95% success rate** required before full ramp
6. **Full ramp**: remaining records replayed through the complete validation pipeline
7. Payments that fail replay are marked `LEGITIMATELY_REJECTED` — they were genuine rejects

---

## Rail-Specific Configuration

Each rail has its own quarantine window, policy thresholds, and scoring weights:

| Rail | Max Quarantine | Threshold | Priority | Rationale |
|---|---|---|---|---|
| **RTP** | 5 min | 65 | Highest | Real-time — minutes matter |
| **Zelle** | 5 min | 65 | Highest | Real-time — minutes matter |
| **Wire** | 1 hr | 60 | High | Same-day finality expectation |
| **FX** | 30 min | 58 | Medium | Rate-lock window constraints |
| **ACH** | 24 hr | 55 | Standard | Batch processing window |

---

## Key Design Decisions

- **Not a DLQ** — SRQ acts *before* bad state escapes the service, not after
- **Not observability** — it changes the outcome, not just the view
- **Not a gateway** — a library import, no infrastructure changes needed
- **SDK latency < 1ms** — all reads from local cache client, zero external I/O in the reject path
- **< 2MB footprint** — per service instance
- **Shadow mode** — Phase 1 deploys with zero enforcement; score every reject, change nothing
- **Conservative defaults** — a false quarantine (holding a legitimate reject) is worse than a false pass; start strict and tune with data

---

## Audit Trail

Every decision is logged with the full signal snapshot — the immutable causal chain regulators and bank examiners require:

- Every `evaluate()` call writes an inline audit entry
- Every quarantine, anomaly detection, recovery poll, canary result, and replay action writes to the audit store
- Every state transition on a `QuarantineRecord` is recorded with timestamp, from-state, to-state, and reason

---

## API Reference

### SRQSDK

```python
sdk = SRQSDK(
    baseline_cache=BaselineCache(),          # pre-warmed baseline store
    policy_evaluator=PolicyEvaluator(),      # per-rail/per-client thresholds
    quarantine_publisher=callback,           # async → control plane event bus
    audit_writer=callback,                   # inline audit log
    shadow_mode=False,                       # Phase 1: observe only
)

decision = sdk.evaluate(candidate)           # → TrustDecision
score    = sdk.trust_score(candidate)        # → int (0–100)
action   = sdk.quarantine_or_pass(candidate) # → TrustAction
```

### SRQControlPlane

```python
cp = SRQControlPlane(
    replay_fn=reprocess_payment,     # True = cleared, False = legitimately rejected
    dependency_health=health_fn,     # → bool
    reject_rate_ok=rate_fn,          # → bool
    cache_fresh=cache_fn,            # → bool
    latency_ok=latency_fn,           # → bool
)

record  = cp.on_quarantine_event(candidate, decision)  # ingest from SDK
actions = cp.run_recovery_cycle()                       # poll + canary + ramp
dash    = cp.dashboard()                                # queue state summary
```

### RejectCandidate

```python
candidate = RejectCandidate.create(
    amount_cents=250_000,
    currency="USD",
    rail=Rail.WIRE,
    originator_id="orig_1234",
    beneficiary_id="bene_5678",
    reject_reason_code="AC04",
    signals=InfraSignals(
        reject_burst_ratio=1.0,
        latency_deviation_ms=0,
        upstream_5xx_rate=0.0,
        cache_miss_rate=0.04,
        dependency_health_score=1.0,
        cert_error_rate=0.0,
    ),
    processing_latency_ms=130,
)
```

---

## What Makes It Different

| Approach | Problem |
|---|---|
| Dead Letter Queue | Acts after bad state escapes — client already notified |
| Observability / Alerts | Shows the problem but doesn't change the outcome |
| Retry loops | Blind retries without root-cause awareness; no quarantine |
| API Gateway rules | External proxy adds latency; can't read service-internal signals |
| **SRQ** | **Intercepts in-process, pre-terminal, with full signal context** |

---

## Dependencies

Python 3.10+ (standard library only — no external packages required).

---

## License

Internal / Proprietary — Meridian Payments Engineering.
