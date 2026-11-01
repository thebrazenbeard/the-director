"""Private, complete-input, replayable advisory dossiers for The Director.

The hashes in this module establish equality to a declared input snapshot.
They DO NOT authenticate evidence, a clock, an actor, approval, or permission.
This module has no filesystem, network, process, or effect adapters. Callers
must explicitly decide whether or where to persist sensitive dossier bytes.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Mapping, Sequence

from .decision import evaluate


SCHEMA = "the-director.dossier/1"
CANONICALIZER = "director-json/1"
POLICY_PROTOCOL = "the-director.decision/0.1"
INTEGRITY = "CONTENT_DIGEST_ONLY"
# Match the optional local custody store's single-record bound. A dossier is
# not written there automatically; callers must opt in to persistence.
MAX_DOSSIER_BYTES = 262_144
MAX_TEXT_CHARS = 4_096
MAX_DEPTH = 16
_RECORD_FIELDS = frozenset({
    "schema", "canonicalizer", "policy_protocol", "integrity", "input",
    "decision", "input_sha256", "decision_sha256", "dossier_sha256",
})
_INPUT_FIELDS = frozenset({"mission", "proposals", "evidence", "now_utc"})
_SET_FIELDS = ("allowed_actions", "allowed_targets", "forbidden_targets")


def _snapshot(value: object, depth: int = 0) -> object:
    """Build independent, strictly JSON-safe data; reject cycles and ambiguity."""
    if depth > MAX_DEPTH:
        raise ValueError("dossier data exceeds maximum nesting depth")
    if value is None or type(value) in (bool, int):
        if type(value) is int and abs(value) > 2**63 - 1:
            raise ValueError("dossier integer exceeds 64-bit range")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("dossier cannot contain NaN or infinity")
        return value
    if type(value) is str:
        if len(value) > MAX_TEXT_CHARS:
            raise ValueError("dossier text is too long")
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ValueError("dossier contains invalid Unicode") from exc
        return value
    if isinstance(value, Mapping):
        if len(value) > 128 or any(type(k) is not str for k in value):
            raise ValueError("dossier mappings require bounded string keys")
        return {_snapshot(k, depth + 1): _snapshot(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        if len(value) > 500:
            raise ValueError("dossier array too large")
        return [_snapshot(v, depth + 1) for v in value]
    raise ValueError("dossier requires strictly JSON-compatible input")


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise ValueError("dossier cannot be canonically serialized") from exc


def _digest(domain: str, value: object) -> str:
    # Domain separation prevents treating an input hash as a dossier hash.
    return hashlib.sha256(domain.encode("ascii") + b"\x00" + _json_bytes(value)).hexdigest()


def _normalize_utc(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise ValueError("mission expiry must be an ISO-8601 UTC string") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("mission expiry must be timezone-aware UTC")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonicalize_input(raw: object) -> dict[str, object]:
    if not isinstance(raw, Mapping) or set(raw) != _INPUT_FIELDS:
        raise ValueError("dossier input must contain all exact input fields")
    data = _snapshot(raw)
    mission = data["mission"]
    proposals = data["proposals"]
    evidence = data["evidence"]
    if not isinstance(mission, dict):
        raise ValueError("mission must be a mapping")
    if not isinstance(proposals, list) or len(proposals) > 100:
        raise ValueError("proposals must be a bounded list")
    if not isinstance(evidence, list) or len(evidence) > 500:
        raise ValueError("evidence must be a bounded list")
    if not all(isinstance(row, dict) for row in proposals + evidence):
        raise ValueError("proposal and evidence rows must be mappings")
    now = data["now_utc"]
    if not isinstance(now, str):
        raise ValueError("now_utc must be an ISO-8601 UTC string")
    data["now_utc"] = _normalize_utc(now)
    if "expires_at_utc" in mission and isinstance(mission["expires_at_utc"], str):
        mission["expires_at_utc"] = _normalize_utc(mission["expires_at_utc"])
    for name in _SET_FIELDS:
        value = mission.get(name)
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            mission[name] = sorted(value)
    for row in proposals:
        references = row.get("evidence_ids")
        if isinstance(references, list) and all(isinstance(item, str) for item in references):
            row["evidence_ids"] = sorted(references)
    proposals.sort(key=lambda row: (str(row.get("proposal_id", "")), _json_bytes(row)))
    evidence.sort(key=lambda row: (str(row.get("evidence_id", "")), _json_bytes(row)))
    if len(_json_bytes(data)) > MAX_DOSSIER_BYTES:
        raise ValueError("dossier input exceeds byte budget")
    return data


def _now_as_utc(now_utc: datetime) -> str:
    if (
        not isinstance(now_utc, datetime) or now_utc.tzinfo is None
        or now_utc.utcoffset() != timedelta(0)
    ):
        raise ValueError("now_utc must be timezone-aware UTC; supplied clock is unverified")
    return now_utc.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _evaluation(data: Mapping[str, object]) -> dict[str, object]:
    now = datetime.fromisoformat(data["now_utc"].replace("Z", "+00:00"))
    result = evaluate(data["mission"], data["proposals"], data["evidence"], now_utc=now)
    if result["protocol"] != POLICY_PROTOCOL:
        raise ValueError("decision protocol incompatible with dossier version")
    if result["authorization_status"] != "NOT_GRANTED" or result["effect_status"] != "NOT_EXECUTED":
        raise ValueError("dossier must remain advisory and non-executing")
    return result


def _bound_record(data: dict[str, object], decision: dict[str, object]) -> dict[str, object]:
    record = {
        "schema": SCHEMA,
        "canonicalizer": CANONICALIZER,
        "policy_protocol": POLICY_PROTOCOL,
        "integrity": INTEGRITY,
        "input": data,
        "decision": decision,
        "input_sha256": _digest("director-input-v1", data),
        "decision_sha256": _digest("director-decision-v1", decision),
    }
    record["dossier_sha256"] = _digest("director-dossier-v1", record)
    if len(_json_bytes(record)) > MAX_DOSSIER_BYTES:
        raise ValueError("dossier record exceeds byte budget")
    return record


def build_dossier(
    mission: Mapping[str, object],
    proposals: Sequence[Mapping[str, object]],
    evidence: Sequence[Mapping[str, object]],
    *,
    now_utc: datetime,
) -> dict[str, object]:
    """Bind all declared inputs to an advisory decision, without persisting them."""
    data = _canonicalize_input({
        "mission": mission,
        "proposals": proposals,
        "evidence": evidence,
        "now_utc": _now_as_utc(now_utc),
    })
    return _bound_record(data, _evaluation(data))


def replay_dossier(dossier: Mapping[str, object]) -> str:
    """Validate canonical input, hashes and reproduced advisory decision.

    MATCH means only local content equality under the installed policy. It does
    not authenticate source identity, evidence truth, a clock, or authority.
    Invalid or incompatible records raise ValueError, never a recommendation.
    """
    if not isinstance(dossier, Mapping) or set(dossier) != _RECORD_FIELDS:
        raise ValueError("dossier schema fields mismatch")
    for field, expected in (("schema", SCHEMA), ("canonicalizer", CANONICALIZER),
                            ("policy_protocol", POLICY_PROTOCOL), ("integrity", INTEGRITY)):
        if dossier[field] != expected:
            raise ValueError(f"incompatible dossier {field}")
    data = _canonicalize_input(dossier["input"])
    if data != dossier["input"]:
        raise ValueError("dossier input not canonical")
    reproduced = _evaluation(data)
    expected = _bound_record(data, reproduced)
    if expected != dossier:
        raise ValueError("dossier content or digest mismatch")
    return "MATCH"


def serialize_dossier(dossier: Mapping[str, object]) -> bytes:
    """Produce bounded canonical UTF-8 JSON after self-consistency validation."""
    replay_dossier(dossier)
    return _json_bytes(dossier)


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key in dossier")
        result[key] = value
    return result


def _reject_constant(token: str) -> object:
    raise ValueError(f"nonfinite number in dossier: {token}")


def parse_dossier(data: bytes) -> dict[str, object]:
    """Reject malformed, noncanonical or tampered untrusted dossier bytes."""
    if type(data) is not bytes or not data or len(data) > MAX_DOSSIER_BYTES:
        raise ValueError("dossier bytes missing or too large")
    try:
        record = json.loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("malformed dossier JSON") from exc
    if data != _json_bytes(record):
        raise ValueError("dossier serialization is not canonical")
    replay_dossier(record)
    return record


def public_receipt(dossier: Mapping[str, object]) -> dict[str, object]:
    """Explicit opt-in, minimal digest receipt; no private free-text inputs.

    Even content hashes may reveal low-entropy inputs by guessing. A caller
    must make an independent decision before publishing this receipt.
    """
    replay_dossier(dossier)
    decision = dossier["decision"]
    return {
        "schema": SCHEMA,
        "integrity": INTEGRITY,
        "policy_protocol": POLICY_PROTOCOL,
        "input_sha256": dossier["input_sha256"],
        "decision_sha256": dossier["decision_sha256"],
        "dossier_sha256": dossier["dossier_sha256"],
        "status": decision["status"],
        "authorization_status": "NOT_GRANTED",
        "effect_status": "NOT_EXECUTED",
    }
