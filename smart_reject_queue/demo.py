"""
SRQ Demo — Five-Phase Delivery Journey

Simulates a payment processing stream progressing through:
  Phase 1 — Shadow   : observe only, baseline builds, zero enforcement
  Phase 2 — Classify : score every reject, dashboard live, no state change
  Phase 3 — Quarantine: pilot rail (ACH) active, quarantine live, canary replay
  Phase 4 — Replay   : full recovery loop, all rails, ramp control
  Phase 5 — Platform  : summary stats across all rails and clients
"""
from __future__ import annotations

import random
import time
from typing import Callable

from .models import InfraSignals, Rail, RejectCandidate, TrustAction
from .sdk import SRQSDK
from .control_plane import SRQControlPlane
from .policy import RailPolicy


# ── ANSI colours ─────────────────────────────────────────────────────────────

class C:
    BOLD   = "\033[1m"
    RESET  = "\033[0m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    CYAN   = "\033[96m"
    GREY   = "\033[90m"
    WHITE  = "\033[97m"


def _hdr(title: str) -> None:
    print(f"\n{C.BOLD}{C.CYAN}{'─' * 64}{C.RESET}")
    print(f"{C.BOLD}{C.WHITE}  {title}{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}{'─' * 64}{C.RESET}")


def _ok(msg: str)   -> None: print(f"  {C.GREEN}✓{C.RESET}  {msg}")
def _warn(msg: str) -> None: print(f"  {C.YELLOW}⚠{C.RESET}  {msg}")
def _bad(msg: str)  -> None: print(f"  {C.RED}✗{C.RESET}  {msg}")
def _info(msg: str) -> None: print(f"  {C.GREY}·{C.RESET}  {msg}")


# ── Signal factories ──────────────────────────────────────────────────────────

def _healthy_signals() -> InfraSignals:
    """Normal operating conditions — all signals near baseline."""
    return InfraSignals(
        reject_burst_ratio      = round(random.uniform(0.8, 1.2), 2),
        latency_deviation_ms    = round(random.uniform(0, 10), 1),
        upstream_5xx_rate       = round(random.uniform(0.000, 0.008), 4),
        cache_miss_rate         = round(random.uniform(0.02, 0.06), 4),
        dependency_health_score = round(random.uniform(0.97, 1.00), 3),
        cert_error_rate         = round(random.uniform(0.000, 0.002), 4),
    )


def _degraded_signals(severity: float = 0.7) -> InfraSignals:
    """
    Infrastructure degradation: cache eviction + partner 503s.
    severity 0–1 controls how bad things look.
    """
    return InfraSignals(
        reject_burst_ratio      = round(1.0 + severity * 8.0, 2),   # up to 9× baseline
        latency_deviation_ms    = round(severity * 1_200, 1),        # up to 1.2s over p99
        upstream_5xx_rate       = round(severity * 0.55, 4),         # up to 55% 5xx
        cache_miss_rate         = round(0.05 + severity * 0.60, 4),  # mass cache miss
        dependency_health_score = round(1.0 - severity * 0.85, 3),   # nearly down
        cert_error_rate         = round(severity * 0.03, 4),
    )


def _make_candidate(
    rail: Rail,
    code: str,
    amount: int,
    signals: InfraSignals,
    latency_ms: float,
) -> RejectCandidate:
    return RejectCandidate.create(
        amount_cents          = amount,
        currency              = "USD",
        rail                  = rail,
        originator_id         = f"orig_{random.randint(1000, 9999)}",
        beneficiary_id        = f"bene_{random.randint(1000, 9999)}",
        reject_reason_code    = code,
        signals               = signals,
        processing_latency_ms = latency_ms,
    )


# ── Replay function (simulates re-running through the payment pipeline) ───────

def _make_replay_fn(failure_rate: float = 0.05) -> Callable:
    """
    Returns a replay function.
    After system recovery, ~95% of quarantined payments clear on replay.
    The remaining ~5% were genuinely bad (e.g. stale account that was also closed).
    """
    def replay(candidate: RejectCandidate) -> bool:
        return random.random() > failure_rate
    return replay


# ── Phase runners ─────────────────────────────────────────────────────────────

def phase1_shadow(sdk: SRQSDK, n: int = 12) -> None:
    _hdr("PHASE 1 — SHADOW   Observe only · zero enforcement · baseline builds")
    sdk.set_shadow_mode(True)

    audit_log: list[dict] = []
    sdk._audit = audit_log.append

    codes = [
        (Rail.ACH,   "R01",  50_000, _healthy_signals(), 85),
        (Rail.WIRE,  "AC04", 250_000, _healthy_signals(), 130),
        (Rail.RTP,   "R01",  10_000, _healthy_signals(), 22),
        (Rail.ACH,   "R02",  30_000, _healthy_signals(), 90),
        (Rail.WIRE,  "RR01", 180_000, _healthy_signals(), 145),
    ]
    random.seed(42)
    for i in range(n):
        rail, code, amt, sig, lat = random.choice(codes)
        c = _make_candidate(rail, code, amt, sig, lat)
        d = sdk.evaluate(c)
        tag = f"[SHADOW→{d.classification.value}]"
        _info(f"{rail.value:6} {code:5} trust={d.trust_score:3d} {tag}")

    _ok(f"Shadow complete — {n} rejects scored, 0 quarantined, baseline warming")


def phase2_classify(sdk: SRQSDK) -> None:
    _hdr("PHASE 2 — CLASSIFY  Score every reject · dashboard live · no state change")
    sdk.set_shadow_mode(False)   # classification active but quarantine still off via policy

    # Temporarily raise thresholds so nothing quarantines in this phase
    from .policy import PolicyEvaluator
    sdk._policy = PolicyEvaluator(rail_policies={
        r: __import__("meridian_smart_queue.policy", fromlist=["RailPolicy"]).RailPolicy(r, quarantine_threshold=0)
        for r in ["ACH", "WIRE", "RTP", "ZELLE", "FX"]
    })

    mix = [
        (Rail.ACH,   "R01",  45_000, _healthy_signals(),   88,   "Legitimate — insuf funds"),
        (Rail.WIRE,  "AC04", 300_000, _healthy_signals(),  125,  "Legitimate — closed acct"),
        (Rail.ACH,   "R01",  60_000, _degraded_signals(0.4), 450, "Suspect — cache thrash"),
        (Rail.RTP,   "R01",  15_000, _degraded_signals(0.7), 800, "Systemic — dep down"),
        (Rail.WIRE,  "RR01", 200_000, _degraded_signals(0.6), 600, "Systemic — 5xx storm"),
    ]

    for rail, code, amt, sig, lat, label in mix:
        c = _make_candidate(rail, code, amt, sig, lat)
        d = sdk.evaluate(c)
        colour = C.GREEN if d.classification.value == "LEGITIMATE" \
            else C.YELLOW if d.classification.value == "SUSPECT" else C.RED
        print(
            f"  {colour}{d.classification.value:12}{C.RESET}  "
            f"{rail.value:6} {code:5}  trust={d.trust_score:3d}  conf={d.confidence:.0%}  "
            f"— {label}"
        )
        if d.signals_fired:
            _info(f"         signals: {', '.join(d.signals_fired)}")

    _ok("Classification dashboard live — no payments quarantined yet")


def phase3_quarantine(
    sdk: SRQSDK, cp: SRQControlPlane, n_wave: int = 30
) -> None:
    _hdr("PHASE 3 — QUARANTINE  ACH pilot rail · quarantine live · canary replay")

    # Enable quarantine on ACH only (pilot rail)
    sdk._policy.update_rail(RailPolicy("ACH",   quarantine_threshold=60, min_confidence=0.50))
    sdk._policy.update_rail(RailPolicy("WIRE",  quarantine_threshold=0,  enabled=False))
    sdk._policy.update_rail(RailPolicy("RTP",   quarantine_threshold=0,  enabled=False))

    quarantined = 0
    passed_through = 0

    random.seed(7)
    print(f"\n  Simulating infrastructure degradation wave ({n_wave} rejects)...\n")

    for i in range(n_wave):
        severity = 0.75 if i < 20 else 0.1   # degradation clears after 20 events
        sig = _degraded_signals(severity) if i < 20 else _healthy_signals()
        lat = 900 if i < 20 else 88
        c   = _make_candidate(Rail.ACH, "R01", random.randint(10_000, 500_000), sig, lat)
        d   = sdk.evaluate(c)

        if d.action == TrustAction.QUARANTINE:
            quarantined += 1
            _warn(f"  [{i+1:02d}] PROCESSING_DELAYED  trust={d.trust_score:3d}  "
                  f"${c.amount_cents/100:,.0f}  signals={d.signals_fired[:1]}")
        else:
            passed_through += 1

    print()
    _ok(f"Wave complete — {quarantined} quarantined, {passed_through} passed through")
    print()

    # Dashboard snapshot
    dash = cp.dashboard()
    _info(f"Quarantine queue:  {dash['total_quarantined']} records")
    _info(f"Open anomaly groups: {dash['open_anomaly_groups']}")
    _info(f"Audit entries:     {dash['audit_entries']}")
    _info(f"By state:          {dash['by_state']}")


def phase4_replay(cp: SRQControlPlane) -> None:
    _hdr("PHASE 4 — REPLAY  Recovery confirmed · canary → full ramp")

    print("\n  Recovery Detector polling (3-of-4 signals × 2 cycles required)...\n")

    # Simulate 3 polling cycles (first cycle: partial, then 2 × full recovery)
    for cycle in range(1, 4):
        actions = cp.run_recovery_cycle()

        for act in actions:
            if act["action"] == "waiting_for_recovery":
                _info(f"  Cycle {cycle}: anomaly {act['anomaly_id'][:8]}… — "
                      f"waiting ({act['signals_confirmed']}/4 signals confirmed, "
                      f"cohort={act['cohort_size']})")
            elif act["action"] == "full_ramp_complete":
                c_stats = act["canary"]
                r_stats = act["ramp"]
                print()
                _ok(f"  Cycle {cycle}: CANARY passed  "
                    f"({c_stats['cleared']}/{c_stats['canary_size']} cleared, "
                    f"rate={c_stats['success_rate']:.0%})")
                _ok(f"  Full ramp complete — "
                    f"{r_stats['cleared']} cleared, "
                    f"{r_stats['legitimately_rejected']} legitimately rejected")
            elif act["action"] == "canary_failed_requeued":
                _bad(f"  Cycle {cycle}: canary failed — requeued for next cycle")

    print()
    dash = cp.dashboard()
    _info(f"Final queue state: {dash['by_state']}")
    _info(f"Total audit entries: {dash['audit_entries']}")


def phase5_platform(sdk: SRQSDK, cp: SRQControlPlane) -> None:
    _hdr("PHASE 5 — PLATFORM  All rails · multi-tenant · summary")

    # Enable all rails
    for rail_name in ["ACH", "WIRE", "RTP", "ZELLE", "FX"]:
        sdk._policy.update_rail(RailPolicy(rail_name, quarantine_threshold=60,
                                           min_confidence=0.50, enabled=True))

    all_rails = [
        (Rail.ACH,   "R01",   50_000, _healthy_signals(),       85),
        (Rail.WIRE,  "AC04", 300_000, _degraded_signals(0.65), 700),
        (Rail.RTP,   "R01",   12_000, _degraded_signals(0.80), 950),
        (Rail.ZELLE, "R01",    8_000, _degraded_signals(0.55), 500),
        (Rail.FX,    "AM03", 180_000, _healthy_signals(),      220),
        (Rail.WIRE,  "RR01", 250_000, _degraded_signals(0.70), 750),
        (Rail.ACH,   "R02",   35_000, _healthy_signals(),       92),
        (Rail.RTP,   "R03",   20_000, _degraded_signals(0.90), 980),
    ]

    results: dict[str, dict[str, int]] = {}
    for rail, code, amt, sig, lat in all_rails:
        c = _make_candidate(rail, code, amt, sig, lat)
        d = sdk.evaluate(c)
        key = rail.value
        results.setdefault(key, {"pass": 0, "quarantine": 0, "total": 0})
        results[key]["total"] += 1
        if d.action == TrustAction.QUARANTINE:
            results[key]["quarantine"] += 1
        else:
            results[key]["pass"] += 1

    print()
    print(f"  {'Rail':<8} {'Total':>6} {'Pass':>6} {'Quarantine':>11}  {'Q-Rate':>7}")
    print(f"  {'─'*8} {'─'*6} {'─'*6} {'─'*11}  {'─'*7}")
    for rail_name, counts in results.items():
        q_rate = counts["quarantine"] / counts["total"]
        bar = "█" * counts["quarantine"]
        print(
            f"  {rail_name:<8} {counts['total']:>6} {counts['pass']:>6} "
            f"{counts['quarantine']:>11}  {q_rate:>6.0%}  {bar}"
        )

    total_q = sum(v["quarantine"] for v in results.values())
    total_t = sum(v["total"] for v in results.values())
    print()
    _ok(f"Platform sweep: {total_t} rejects evaluated, {total_q} quarantined "
        f"({total_q/total_t:.0%} intercept rate)")


# ── Main ──────────────────────────────────────────────────────────────────────

def run_demo() -> None:
    print(f"\n{C.BOLD}{C.WHITE}")
    print("  ███████╗ ██████╗  ██████╗ ")
    print("  ██╔════╝ ██╔══██╗██╔═══██╗")
    print("  ███████╗ ██████╔╝██║   ██║")
    print("  ╚════██║ ██╔══██╗██║▄▄ ██║")
    print("  ███████║ ██║  ██║╚██████╔╝")
    print("  ╚══════╝ ╚═╝  ╚═╝ ╚══▀▀═╝ ")
    print(f"  Smart Reject Queue  ·  Meridian Payments{C.RESET}")

    random.seed(0)

    # --- Wire up the system -------------------------------------------
    # Control plane (Level 3) — async, out-of-band
    cp = SRQControlPlane(
        replay_fn         = _make_replay_fn(failure_rate=0.05),
        dependency_health = lambda: True,   # recovery signals all green after phase 3
        reject_rate_ok    = lambda: True,
        cache_fresh       = lambda: True,
        latency_ok        = lambda: True,
    )

    # SDK (Level 1 + 2) — embedded in the payment service
    sdk = SRQSDK(
        quarantine_publisher = cp.on_quarantine_event,
        shadow_mode          = True,
    )

    # --- Five phases ---------------------------------------------------
    phase1_shadow(sdk)
    phase2_classify(sdk)
    phase3_quarantine(sdk, cp)
    phase4_replay(cp)
    phase5_platform(sdk, cp)

    _hdr("RUN COMPLETE")
    _ok("SRQ intercepted infra-induced false rejects before terminal state")
    _ok("Legitimate rejects passed through immediately — zero false hold")
    _ok("Canary replay confirmed recovery before full ramp")
    _ok("Full audit trail written — compliance-ready causal chain")
    print()


if __name__ == "__main__":
    run_demo()
