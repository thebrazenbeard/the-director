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
import json
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


# Inventory is deliberately incapable of representing a passing security test.
# Do not add signatures or caller approval flags here: custody and verification
# need a separately reviewed native host implementation, not JSON assertions.
HOST_INVENTORY_MAX_BYTES = 4096
HOST_QUALIFICATION_GAPS = (
    "WINDOWS_NATIVE_CREDENTIAL_BOUND_BROKER",
    "DISTINCT_BROKER_WORKER_SIDS",
    "BROKER_KEY_AND_DATABASE_ACLS",
    "NAMED_PIPE_PEER_BINDING",
    "JOB_OBJECT_PROCESS_TREE_CLEANUP",
    "RESOURCE_AND_RESTART_CAPS",
    "PAUSE_AND_STOP_CONTROLS",
    "ONE_INSTANCE_LOCK",
    "SLEEP_AND_REBOOT_RECOVERY",
    "RECEIPT_ACK_CRASH_RESTART_MATRIX",
    "REVOCATION_CRASH_RESTART_MATRIX",
    "INDEPENDENT_COLLECTION_PRINCIPAL",
    "TAMPER_RESISTANT_EVIDENCE_CUSTODY",
    "HOST_BOOT_BINARY_CONFIG_COMMIT_BINDING",
    "FRESHNESS_AND_INDEPENDENT_VERIFICATION",
)


def _inventory(raw: bytes) -> dict[str, object]:
    """Accept only bounded, nonidentifying inventory; never accept authority."""
    if type(raw) is not bytes or not 0 < len(raw) <= HOST_INVENTORY_MAX_BYTES:
        raise ValueError("invalid inventory")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("invalid inventory")
            result[key] = value
        return result

    def nonfinite(_):
        raise ValueError("invalid inventory")

    data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                      parse_constant=nonfinite)
    keys = {"schema", "provenance", "platform", "os_build",
            "logical_processors", "powershell_major", "process_64bit"}
    if type(data) is not dict or set(data) != keys:
        raise ValueError("invalid inventory")
    if (data["schema"] != "director.host-inventory.v1"
            or data["provenance"] != "REAL_HOST_PROBE"
            or data["platform"] != "Windows"):
        raise ValueError("unsupported inventory")
    for key, maximum in (("os_build", 1000000), ("logical_processors", 65536),
                         ("powershell_major", 100)):
        if type(data[key]) is not int or not 1 <= data[key] <= maximum:
            raise ValueError("invalid inventory")
    if type(data["process_64bit"]) is not bool:
        raise ValueError("invalid inventory")
    return data


def assess_host_inventory(raw: bytes | None = None) -> dict[str, object]:
    """Pure source-level report, NEVER a release, service or execution gate.

    A structurally valid snapshot can still be forged, stale, copied, or from
    the wrong host/boot/commit. We cannot establish any of these bindings and
    deliberately never upgrade a snapshot to independently verified evidence.
    Invalid inputs are not echoed, including exceptions or unknown fields.
    """
    inventory = None
    disposition = "NOT_PROVIDED"
    if raw is not None:
        try:
            inventory = _inventory(raw)
            disposition = "UNVERIFIED_INVENTORY_ONLY"
        except (ValueError, TypeError, UnicodeError, RecursionError):
            disposition = "REJECTED_INPUT"
    return {
        "schema": "director.host-readiness.v1",
        "status": "UNQUALIFIED_HOST",
        "qualified": False,
        "release_decision": "NO_GO",
        "effect_authority": "NOT_GRANTED",
        "effect_status": "NOT_EXECUTED",
        "inventory_disposition": disposition,
        "inventory": inventory,
        "evidence_provenance": {
            "DOC": "CHECKLIST_ONLY",
            "SOURCE": "FAIL_CLOSED_RENDERER",
            "TEST": "NO_HOST_PROOF",
            "REAL_HOST_PROBE": "UNVERIFIED_SNAPSHOT" if inventory else "NOT_AVAILABLE",
        },
        "checks": [{"code": code, "status": "NOT_TESTED", "evidence": "UNPROVEN"}
                   for code in HOST_QUALIFICATION_GAPS],
    }


def render_host_readiness(raw: bytes | None = None) -> str:
    """Return bounded JSON with no paths, credentials, or input error text."""
    return json.dumps(assess_host_inventory(raw), sort_keys=True, indent=2)


__all__ += ["HOST_INVENTORY_MAX_BYTES", "HOST_QUALIFICATION_GAPS",
            "assess_host_inventory", "render_host_readiness"]
