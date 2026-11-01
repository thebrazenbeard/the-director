"""Read-only, fail-closed qualification for a hypothetical unattended Director.

This module does not run a loop, create threads, schedule tasks, open sockets,
mutate coordinator state, inspect host credentials, or grant execution rights.
It only examines an existing coordinator's ``inspect()`` projection. Crucial
OS-principal isolation and worker receipt/recovery evidence are unavailable
from that projection, so even an otherwise suitable coordinator is *never*
declared qualified for an unattended Lappy deployment by this API.

This is a source-only readiness contract, **not** a test of the actual Lappy
host, Windows Task Scheduler, host uptime, or cross-device communication.
Its timestamp is supplied by the caller and is not an independently trusted
clock. It cannot certify that persisted outbound signed intents are unexpired.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Protocol, TypedDict


_Boundary = Literal["COORDINATOR", "AUTHENTICATION", "RECOVERY", "HOST", "PROTOCOL"]


class ReadinessBlocker(TypedDict):
    code: str
    boundary: _Boundary
    detail: str


class UnattendedReadiness(TypedDict):
    schema: Literal["director.unattended-readiness.v1"]
    status: Literal["BLOCKED", "UNQUALIFIED_HOST"]
    qualified: Literal[False]
    effect_authority: Literal["NOT_GRANTED"]
    effect_status: Literal["NOT_EXECUTED"]
    host_probe: Literal["NOT_PERFORMED"]
    declared_now_utc: int
    coordinator_status: str
    authentication_mode: str
    blockers: list[ReadinessBlocker]


class _Inspectable(Protocol):
    def inspect(self) -> Mapping[str, object]: ...


_MAX_UTC = 4102444800  # 2100-01-01, matching the HMAC coordinator time bound.
_LIFECYCLE = frozenset({
    "RUNNING", "PAUSED", "OBJECTIVE_MET", "UNRESOLVED", "EXHAUSTED", "QUARANTINED",
})


def assess_unattended_readiness(
    coordinator: _Inspectable, *, now_utc: int,
) -> UnattendedReadiness:
    """Report conservative, bounded blockers without changing local state.

    This receipt can help an operator decide *what to qualify next*, but is
    never permission to start a supervisor or worker. In particular the API
    cannot see the host's principal isolation, message-processing durability,
    or actual Task Scheduler and power-management configuration.
    """
    if type(now_utc) is not int or not 0 <= now_utc <= _MAX_UTC:
        raise ValueError("now_utc must be a bounded UTC POSIX second")

    blockers: list[ReadinessBlocker] = []

    def block(code: str, boundary: _Boundary, detail: str) -> None:
        blockers.append({"code": code, "boundary": boundary, "detail": detail})

    try:
        projection = coordinator.inspect()
    except Exception:
        # Deliberately omit errors that could contain paths or sensitive data.
        projection = None
    immediate = False
    state, mode = "UNKNOWN", "UNKNOWN"
    if not isinstance(projection, Mapping):
        block("COORDINATOR_INSPECTION_UNAVAILABLE", "COORDINATOR",
              "A trustworthy read-only coordinator projection was not available.")
        immediate = True
    else:
        candidate_state = projection.get("status")
        if type(candidate_state) is str and candidate_state in _LIFECYCLE:
            state = candidate_state
        candidate_mode = projection.get("authentication_mode")
        if type(candidate_mode) is str and candidate_mode in (
            "ENFORCED_LOCAL_HMAC_BIDIRECTIONAL", "ENFORCED_LOCAL_HMAC", "UNAUTHENTICATED_LOCAL"
        ):
            mode = candidate_mode

        if projection.get("recovery_quarantine") is not None or state == "QUARANTINED":
            block("RECOVERY_QUARANTINED", "RECOVERY",
                  "A quarantined coordinator has no approved reactivation route.")
            immediate = True
        controls = projection.get("operator_control")
        if (not isinstance(controls, Mapping)
                or type(controls.get("paused")) is not bool
                or type(controls.get("stop_requested")) is not bool):
            block("OPERATOR_CONTROL_UNAVAILABLE", "COORDINATOR",
                  "A valid paused/stop-requested operator-control projection is required.")
            immediate = True
        else:
            if controls["stop_requested"]:
                block("OPERATOR_STOP_REQUESTED", "COORDINATOR",
                      "The operator has requested a stop; unattended work must not begin.")
                immediate = True
            if controls["paused"]:
                block("OPERATOR_PAUSED", "COORDINATOR",
                      "The coordinator is paused; unattended work must not begin.")
                immediate = True
        if state != "RUNNING" and state != "QUARANTINED":
            block("COORDINATOR_NOT_RUNNING", "COORDINATOR",
                  "Only a coordinator reporting RUNNING could be considered for future supervision.")
            immediate = True
        if mode != "ENFORCED_LOCAL_HMAC_BIDIRECTIONAL":
            block("BIDIRECTIONAL_AUTH_NOT_ENFORCED", "AUTHENTICATION",
                  "New unattended operation requires a fresh bidirectionally signed coordinator.")
            immediate = True

    # No software-only inspection can promote these host and protocol claims.
    block("HOST_PRINCIPAL_ISOLATION_UNVERIFIED", "HOST",
          "Independent host-enforced worker identity and mailbox access are not qualified.")
    block("WORKER_ACK_RECOVERY_UNVERIFIED", "PROTOCOL",
          "Worker verify/process/ack durability and duplicate-effect controls are unqualified.")
    block("LAPPY_UNATTENDED_RUNTIME_UNVERIFIED", "HOST",
          "No Lappy scheduler, power-state, host uptime or resource-limits probe was performed.")

    return {
        "schema": "director.unattended-readiness.v1",
        "status": "BLOCKED" if immediate else "UNQUALIFIED_HOST",
        "qualified": False,
        "effect_authority": "NOT_GRANTED",
        "effect_status": "NOT_EXECUTED",
        "host_probe": "NOT_PERFORMED",
        "declared_now_utc": now_utc,
        "coordinator_status": state,
        "authentication_mode": mode,
        "blockers": blockers,
    }


__all__ = ["ReadinessBlocker", "UnattendedReadiness", "assess_unattended_readiness"]
