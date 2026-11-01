"""Deterministic, offline recommendation-only policy for The Director.

All inputs are declared data, not authenticated identity, approvals, or commands.
The implementation has no I/O or effect adapter. In particular, an admissible
proposal does not become an authorized or executed action.

The `source_ref` and `lineage_id` fields are operator-supplied *claims* of
source and ancestry, not independently authenticated corroboration. Receipts
are neither signed nor cryptographically bound to the complete input dossier;
they must not be used as approval/replay tokens. Host-side evidence custody,
input integrity, and authorization are out of scope for this reference.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping, Sequence


_IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,63}\Z")
_MISSION_FIELDS = frozenset({
    "mission_id", "revision", "objective", "allowed_actions",
    "allowed_targets", "forbidden_targets", "max_cost_cents",
    "min_independent_evidence", "expires_at_utc",
})
_PROPOSAL_FIELDS = frozenset({
    "proposal_id", "action", "target", "cost_cents", "benefit_points",
    "evidence_ids",
})
_EVIDENCE_FIELDS = frozenset({
    "evidence_id", "evidence_class", "source_ref", "lineage_id",
})
_EVIDENCE_CLASSES = frozenset({
    "OBSERVATION", "INFERENCE", "HYPOTHESIS", "MODEL", "BELIEF", "UNKNOWN",
})
_MAX_PROPOSALS = 100
_MAX_EVIDENCE = 500


@dataclass(frozen=True)
class _Mission:
    mission_id: str
    revision: int
    allowed_actions: frozenset[str]
    allowed_targets: frozenset[str]
    forbidden_targets: frozenset[str]
    max_cost_cents: int
    min_independent_evidence: int
    expires_at_utc: datetime


@dataclass(frozen=True)
class _Evidence:
    evidence_id: str
    evidence_class: str
    source_ref: str
    lineage_id: str


def _mapping(value: object, expected_keys: frozenset[str], label: str) -> Mapping:
    if not isinstance(value, Mapping) or set(value) != expected_keys:
        raise ValueError(f"{label} must contain exactly the documented fields")
    return value


def _id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a bounded identifier")
    return value


def _ids(value: object, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be an identifier list")
    if (not value and not allow_empty) or len(value) > 100:
        raise ValueError(f"{label} must have a bounded number of items")
    result = tuple(_id(item, label) for item in value)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} must not contain duplicates")
    return result


def _integer(value: object, label: str, lower: int, upper: int) -> int:
    # bool is an int subclass, but must never satisfy a numerical policy field.
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f"{label} must be an integer in [{lower}, {upper}]")
    return value


def _utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("expires_at_utc must be an ISO-8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("expires_at_utc must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("expires_at_utc must be timezone-aware UTC")
    return parsed.astimezone(timezone.utc)


def _mission(raw: object) -> _Mission:
    record = _mapping(raw, _MISSION_FIELDS, "mission")
    objective = record["objective"]
    if not isinstance(objective, str) or not objective.strip() or len(objective) > 1000:
        raise ValueError("objective must be nonempty bounded text")
    actions = _ids(record["allowed_actions"], "allowed_actions")
    targets = _ids(record["allowed_targets"], "allowed_targets")
    forbidden = _ids(record["forbidden_targets"], "forbidden_targets", allow_empty=True)
    return _Mission(
        mission_id=_id(record["mission_id"], "mission_id"),
        revision=_integer(record["revision"], "revision", 1, 2**31 - 1),
        allowed_actions=frozenset(actions),
        allowed_targets=frozenset(targets),
        forbidden_targets=frozenset(forbidden),
        max_cost_cents=_integer(record["max_cost_cents"], "max_cost_cents", 0, 2**63 - 1),
        min_independent_evidence=_integer(
            record["min_independent_evidence"], "min_independent_evidence", 1, 100
        ),
        expires_at_utc=_utc(record["expires_at_utc"]),
    )


def _evidence(records: object) -> dict[str, _Evidence]:
    if not isinstance(records, (list, tuple)) or len(records) > _MAX_EVIDENCE:
        raise ValueError("evidence must be a bounded list")
    seen: dict[str, _Evidence] = {}
    for raw in records:
        entry = _mapping(raw, _EVIDENCE_FIELDS, "evidence")
        evidence_id = _id(entry["evidence_id"], "evidence_id")
        source = _id(entry["source_ref"], "source_ref")
        lineage = _id(entry["lineage_id"], "lineage_id")
        if (
            not isinstance(entry["evidence_class"], str)
            or entry["evidence_class"] not in _EVIDENCE_CLASSES
        ):
            raise ValueError("unknown evidence_class")
        if evidence_id in seen:
            raise ValueError("duplicate evidence_id")
        seen[evidence_id] = _Evidence(
            evidence_id=evidence_id,
            evidence_class=entry["evidence_class"],
            source_ref=source,
            lineage_id=lineage,
        )
    return seen


def _assess(
    raw: Mapping[str, object],
    policy: _Mission,
    evidence_by_id: Mapping[str, _Evidence],
    duplicate_ids: set[str],
    expired: bool,
) -> tuple[dict[str, object], tuple[int, int, str] | None]:
    reasons: list[str] = []
    # evaluate() rejects invalid IDs before any assessment is formed, so no
    # fabricated fallback ID can collide with a genuine proposal.
    proposal_id = _id(raw["proposal_id"], "proposal_id")
    if set(raw) != _PROPOSAL_FIELDS:
        reasons.append("UNSUPPORTED_PROPOSAL_FIELDS")
        return (
            {"proposal_id": proposal_id, "admissible": False, "reasons": sorted(set(reasons))},
            None,
        )
    if proposal_id in duplicate_ids:
        reasons.append("DUPLICATE_PROPOSAL_ID")
    try:
        action = _id(raw["action"], "action")
        if action not in policy.allowed_actions:
            reasons.append("ACTION_NOT_ALLOWED")
    except ValueError:
        reasons.append("ACTION_INVALID")
    try:
        target = _id(raw["target"], "target")
        if target in policy.forbidden_targets:
            reasons.append("FORBIDDEN_TARGET")
        if target not in policy.allowed_targets:
            reasons.append("TARGET_NOT_ALLOWED")
    except ValueError:
        reasons.append("TARGET_INVALID")
    try:
        cost = _integer(raw["cost_cents"], "cost_cents", 0, 2**63 - 1)
        if cost > policy.max_cost_cents:
            reasons.append("COST_EXCEEDS_MISSION_BUDGET")
    except ValueError:
        cost = 0
        reasons.append("COST_INVALID")
    try:
        benefit = _integer(raw["benefit_points"], "benefit_points", 0, 100)
    except ValueError:
        benefit = 0
        reasons.append("BENEFIT_INVALID")
    try:
        evidence_ids = _ids(raw["evidence_ids"], "evidence_ids")
        missing = [item for item in evidence_ids if item not in evidence_by_id]
        if missing:
            reasons.append("MISSING_EVIDENCE")
        else:
            observations = [
                evidence_by_id[item] for item in evidence_ids
                if evidence_by_id[item].evidence_class == "OBSERVATION"
            ]
            # A declared source or a declared ancestry may still be forged.
            # This is a fail-closed consistency check, not attestation. Every
            # counted observation must have a unique source AND lineage.
            if (
                len(observations) < policy.min_independent_evidence
                or len({item.lineage_id for item in observations}) != len(observations)
                or len({item.source_ref for item in observations}) != len(observations)
            ):
                reasons.append("INSUFFICIENT_INDEPENDENT_EVIDENCE")
    except ValueError:
        reasons.append("EVIDENCE_REFS_INVALID")
    if expired:
        reasons.append("MISSION_EXPIRED")
    approved = not reasons
    assessment: dict[str, object] = {
        "proposal_id": proposal_id,
        "admissible": approved,
        "reasons": sorted(set(reasons)),
    }
    # Benefit is an input estimate, not an observed outcome or an authorization.
    score = (-benefit, cost, proposal_id) if approved else None
    return assessment, score


def evaluate(
    mission: Mapping[str, object],
    proposals: Sequence[Mapping[str, object]],
    evidence: Sequence[Mapping[str, object]],
    *,
    now_utc: datetime,
) -> dict[str, object]:
    """Return a JSON-safe, deterministic advisory receipt, never an effect.

    A trusted caller must supply a timezone-aware UTC `now_utc`. All amounts
    are nonnegative integer cents; benefit points are unverified estimates
    between 0 and 100. Unknown policy fields or authority claims fail closed.
    Evidence lineages are declared, not independently authenticated.
    """
    if (
        not isinstance(now_utc, datetime)
        or now_utc.tzinfo is None
        or now_utc.utcoffset() != timedelta(0)
    ):
        raise ValueError("now_utc must be a trusted timezone-aware UTC datetime")
    if not isinstance(proposals, (list, tuple)) or len(proposals) > _MAX_PROPOSALS:
        raise ValueError("proposals must be a bounded list")
    policy = _mission(mission)
    evidence_by_id = _evidence(evidence)
    ids = []
    for raw in proposals:
        if not isinstance(raw, Mapping):
            raise ValueError("each proposal must be a mapping")
        ids.append(_id(raw.get("proposal_id"), "proposal_id"))
    duplicate_ids = {key for key, count in Counter(ids).items() if count > 1}
    expired = now_utc >= policy.expires_at_utc
    results = [
        _assess(raw, policy, evidence_by_id, duplicate_ids, expired)
        for raw in proposals
    ]
    results.sort(key=lambda row: (row[0]["proposal_id"], tuple(row[0]["reasons"])))
    admissible = [(score, assessment) for assessment, score in results if score is not None]
    recommendation = min(admissible, key=lambda item: item[0])[1]["proposal_id"] if admissible else None
    return {
        "protocol": "the-director.decision/0.1",
        "mission_id": policy.mission_id,
        "mission_revision": policy.revision,
        "evaluated_at_utc": now_utc.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "RECOMMENDATION_ONLY" if recommendation is not None else "ABSTAIN",
        "recommended_proposal_id": recommendation,
        "assessments": [row[0] for row in results],
        "requires_human_authorization": True,
        "authorization_status": "NOT_GRANTED",
        "effect_status": "NOT_EXECUTED",
        "evidence_custody": "DECLARED_UNVERIFIED",
    }
