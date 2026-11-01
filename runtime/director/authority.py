"""NON-EXECUTING reference checks for a hypothetical host authorization grant.

This module is **not** an authorization service, grant verifier, credential
issuer, replay defense, or effect adapter. Every input is untrusted declared
data, including `issuer_id`, the actor, both SHA-256 claims (the complete
dossier and action parameters), the clock/epoch caller, and every claimed
grant field. Digests are compared for exact equality, but their provenance
and the bytes allegedly hashed are not verified. The best possible result is
``REQUIRES_HOST_ATTESTATION``, *never* authorization to act.

A future trusted host would separately need to authenticate its grantor and
actor, establish the grantor's actual rights, bind the request to a controlled
action implementation, verify protected cryptographic material, atomically
consume nonces, consult durable revocation state, and recheck all restrictions
at execution time. None of that is provided or simulated here.

Only Python standard library pure computation is used; no file, network,
process, clock, token issuance, or external side effects occur.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Mapping


POLICY_VERSION = "director.authority.v1"

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_NONCE = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
_UTC_TEXT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)\Z")

_REQUEST_FIELDS = frozenset({
    "mission_id", "mission_revision", "dossier_sha256", "proposal_id", "actor_id",
    "action_class", "target_id", "parameters_sha256", "audience",
})
_GRANT_FIELDS = frozenset({
    "policy_version", "grant_id", "issuer_id", "subject_actor_id",
    "mission_id", "mission_revision", "dossier_sha256", "proposal_id", "action_class",
    "target_id", "parameters_sha256", "audience", "not_before_utc",
    "expires_at_utc", "revocation_epoch", "revoked_at_utc", "nonce",
})
_SUBJECT_BINDINGS = {
    "mission_id": "mission_id",
    "mission_revision": "mission_revision",
    "dossier_sha256": "dossier_sha256",
    "proposal_id": "proposal_id",
    "actor_id": "subject_actor_id",
    "action_class": "action_class",
    "target_id": "target_id",
    "parameters_sha256": "parameters_sha256",
    "audience": "audience",
}


def _id(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _positive_revision(value: object) -> bool:
    return type(value) is int and 1 <= value < 2**31


def _sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _request_valid(value: object) -> bool:
    if type(value) is not dict or set(value) != _REQUEST_FIELDS:
        return False
    return all(
        _id(value[key])
        for key in _REQUEST_FIELDS - {"mission_revision", "dossier_sha256", "parameters_sha256"}
    ) and _positive_revision(value["mission_revision"]) and _sha256(value["parameters_sha256"]) and _sha256(value["dossier_sha256"])


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or _UTC_TEXT.fullmatch(value) is None:
        return None
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if date.tzinfo is None or date.utcoffset() != timedelta(0):
        return None
    return date.astimezone(timezone.utc)


def _grant_valid(value: object) -> bool:
    if type(value) is not dict or set(value) != _GRANT_FIELDS:
        return False
    if not all(
        _id(value[key])
        for key in _GRANT_FIELDS - {
            "policy_version", "mission_revision", "dossier_sha256", "parameters_sha256",
            "not_before_utc", "expires_at_utc", "revocation_epoch",
            "revoked_at_utc", "nonce",
        }
    ):
        return False
    if not isinstance(value["policy_version"], str) or len(value["policy_version"]) > 64:
        return False
    if (
        not _positive_revision(value["mission_revision"])
        or not _sha256(value["parameters_sha256"])
        or not _sha256(value["dossier_sha256"])
    ):
        return False
    if not isinstance(value["nonce"], str) or _NONCE.fullmatch(value["nonce"]) is None:
        return False
    if type(value["revocation_epoch"]) is not int or not 0 <= value["revocation_epoch"] < 2**31:
        return False
    if _parse_utc(value["not_before_utc"]) is None or _parse_utc(value["expires_at_utc"]) is None:
        return False
    return value["revoked_at_utc"] is None or _parse_utc(value["revoked_at_utc"]) is not None


def _receipt(status: str, reasons: list[str]) -> dict[str, object]:
    # No user-controlled grant value is echoed into a receipt or promoted to
    # evidence of identity, authentication, signature or actual permission.
    return {
        "policy_version": POLICY_VERSION,
        "status": status,
        "reason_codes": sorted(set(reasons)),
        "authorization_status": "NOT_GRANTED",
        "effect_status": "NOT_EXECUTED",
        "verified_grant": False,
    }


def evaluate_reference_authority(
    action_request: Mapping[str, object],
    grant_envelope: Mapping[str, object] | None,
    *,
    now_utc: datetime,
    declared_revocation_epoch: int,
) -> dict[str, object]:
    """Check a proposed exact action binding and untrusted grant metadata.

    `now_utc` and `declared_revocation_epoch` are explicitly caller-supplied
    *assumptions*, not trusted clock or revocation-ledger evidence. Invalid
    context raises ``ValueError``; invalid user input returns ``DENIED``.

    A structurally matching, time-bounded envelope returns
    ``REQUIRES_HOST_ATTESTATION`` because this function deliberately has no
    identity, key, nonce-consumption, durable revocation or effect boundary.
    An envelope never authorizes execution, irrespective of claimed role.
    """
    if (
        type(now_utc) is not datetime or now_utc.tzinfo is None
        or now_utc.utcoffset() != timedelta(0)
    ):
        raise ValueError("now_utc must be a timezone-aware UTC datetime")
    if type(declared_revocation_epoch) is not int or not 0 <= declared_revocation_epoch < 2**31:
        raise ValueError("declared_revocation_epoch must be a bounded nonnegative integer")

    if not _request_valid(action_request):
        return _receipt("DENIED", ["ACTION_REQUEST_SCHEMA_INVALID"])
    if grant_envelope is None:
        return _receipt("DENIED", ["GRANT_MISSING"])
    if not _grant_valid(grant_envelope):
        return _receipt("DENIED", ["GRANT_SCHEMA_INVALID"])

    reasons: list[str] = []
    if grant_envelope["policy_version"] != POLICY_VERSION:
        reasons.append("POLICY_VERSION_UNSUPPORTED")
    if any(
        action_request[action_field] != grant_envelope[grant_field]
        for action_field, grant_field in _SUBJECT_BINDINGS.items()
    ):
        reasons.append("ACTION_SUBJECT_MISMATCH")

    start = _parse_utc(grant_envelope["not_before_utc"])
    end = _parse_utc(grant_envelope["expires_at_utc"])
    # _grant_valid already proved both parses succeeded.
    assert start is not None and end is not None
    if start >= end:
        reasons.append("GRANT_TIME_WINDOW_INVALID")
    else:
        if now_utc < start:
            reasons.append("GRANT_NOT_YET_VALID")
        if now_utc >= end:
            reasons.append("GRANT_EXPIRED")
    if grant_envelope["revoked_at_utc"] is not None:
        reasons.append("GRANT_MARKED_REVOKED")
    if grant_envelope["revocation_epoch"] != declared_revocation_epoch:
        reasons.append("REVOCATION_EPOCH_MISMATCH")
    if reasons:
        return _receipt("DENIED", reasons)

    # Only a future independent, authenticated host policy and atomic nonce
    # ledger can decide whether this structurally plausible claim is real.
    return _receipt("REQUIRES_HOST_ATTESTATION", [
        "ISSUER_NOT_AUTHENTICATED",
        "ACTOR_NOT_AUTHENTICATED",
        "ISSUER_AUTHORITY_NOT_VERIFIED",
        "GRANT_SIGNATURE_NOT_VERIFIED",
        "NONCE_REPLAY_NOT_CHECKED",
        "REVOCATION_LEDGER_NOT_CHECKED",
        "EFFECT_POLICY_NOT_ATTESTED",
    ])
