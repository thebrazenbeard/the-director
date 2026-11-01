"""Bounded, deterministic evidence reducer for a local Director-style planner.

This module holds *declared* reports, not independently verified facts. A
``source_ref`` or ``lineage_id`` names the reporter's asserted provenance; it
does not authenticate the reporter, establish independence, or authorize an
action. No network, filesystem, clock, model, or execution adapter is used.

In particular, a simulation, inference, or hypothesis cannot become an
``OBSERVED`` state simply because it is repeated or conflicts with another
report. Conflicting observations remain explicitly ``CONTESTED``.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any


_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_EVENT_FIELDS = frozenset(
    {"event_id", "claim_id", "value", "evidence_class", "source_ref", "lineage_id"}
)
_EVIDENCE_CLASSES = frozenset({"OBSERVATION", "INFERENCE", "HYPOTHESIS"})
_MAX_EVENTS = 1024
_MAX_CLAIMS = 512
_MAX_TEXT = 512
_MAX_MAGNITUDE = 10**12


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise ValueError(f"{label} must be a bounded identifier")
    return value


def _scalar(value: object) -> bool | int | float | str:
    if isinstance(value, bool):
        return value
    if type(value) is int and -_MAX_MAGNITUDE <= value <= _MAX_MAGNITUDE:
        return value
    if type(value) is float and math.isfinite(value) and abs(value) <= _MAX_MAGNITUDE:
        return value
    if isinstance(value, str) and len(value) <= _MAX_TEXT:
        return value
    raise ValueError("value must be a bounded JSON scalar (not null)")


def _validated(event: Mapping[str, object]) -> dict[str, Any]:
    if not isinstance(event, Mapping) or set(event) != _EVENT_FIELDS:
        raise ValueError("event must contain exactly the documented fields")
    evidence_class = event["evidence_class"]
    if not isinstance(evidence_class, str) or evidence_class not in _EVIDENCE_CLASSES:
        raise ValueError("evidence_class must be OBSERVATION, INFERENCE, or HYPOTHESIS")
    return {
        "event_id": _identifier(event["event_id"], "event_id"),
        "claim_id": _identifier(event["claim_id"], "claim_id"),
        "value": _scalar(event["value"]),
        "evidence_class": evidence_class,
        "source_ref": _identifier(event["source_ref"], "source_ref"),
        "lineage_id": _identifier(event["lineage_id"], "lineage_id"),
    }


def _value_key(value: bool | int | float | str) -> tuple[str, str]:
    # JSON alone would conflate True and 1 in Python collection comparisons;
    # the explicit type also prevents such an unintended consensus.
    return type(value).__name__, json.dumps(value, allow_nan=False, ensure_ascii=True)


class WorldState:
    """Pure in-memory event reducer with a canonical JSON-compatible snapshot.

    ``ingest`` validates exactly six event fields and returns the new snapshot.
    Replaying an identical event ID and identical content is a no-op. Replaying
    the same ID with different content raises instead of rewriting history.
    ``from_snapshot`` rejects altered projections and replays each event.

    A snapshot contains ``revision``, ``facts``, and ``events``. ``facts`` are
    projections of reported claims, *not* a source of verified authority.
    """

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []
        self._by_id: dict[str, dict[str, Any]] = {}
        self._by_claim: dict[str, list[dict[str, Any]]] = {}

    @property
    def revision(self) -> int:
        """Number of distinct events accepted; unaffected by duplicate replay."""
        return len(self._events)

    def ingest(self, event: Mapping[str, object]) -> dict[str, Any]:
        """Accept one declared observation/inference/hypothesis and reduce it."""
        validated = _validated(event)
        event_id = validated["event_id"]
        if event_id in self._by_id:
            previous = self._by_id[event_id]
            if (
                previous != validated
                or _value_key(previous["value"]) != _value_key(validated["value"])
            ):
                raise ValueError("event_id already exists with conflicting content")
            return self.snapshot()

        if len(self._events) >= _MAX_EVENTS:
            raise ValueError("event limit reached")
        claim_id = validated["claim_id"]
        if claim_id not in self._by_claim and len(self._by_claim) >= _MAX_CLAIMS:
            raise ValueError("claim limit reached")

        self._events.append(validated)
        self._by_id[event_id] = validated
        self._by_claim.setdefault(claim_id, []).append(validated)
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        """Return a detached, JSON-compatible view in deterministic order."""
        facts: dict[str, dict[str, Any]] = {}
        for claim_id, records in sorted(self._by_claim.items()):
            observations = [r for r in records if r["evidence_class"] == "OBSERVATION"]
            inferences = [r for r in records if r["evidence_class"] == "INFERENCE"]
            hypotheses = [r for r in records if r["evidence_class"] == "HYPOTHESIS"]
            if observations:
                active, status = observations, "OBSERVED"
            elif inferences:
                active, status = inferences, "INFERRED"
            else:
                active, status = hypotheses, "HYPOTHETICAL"

            values = {_value_key(record["value"]) for record in active}
            if len(values) != 1:
                status, value = "CONTESTED", None
            else:
                value = active[0]["value"]

            facts[claim_id] = {
                "value": value,
                "status": status,
                "evidence_ids": sorted(r["event_id"] for r in records),
                "source_refs": sorted({r["source_ref"] for r in records}),
                "lineage_ids": sorted({r["lineage_id"] for r in records}),
                "evidence_classes": sorted({r["evidence_class"] for r in records}),
            }
        return {
            "revision": self.revision,
            "facts": facts,
            "events": [dict(event) for event in self._events],
        }

    @classmethod
    def from_snapshot(cls, raw: Mapping[str, object]) -> "WorldState":
        """Reconstruct only a snapshot whose derived state matches its events."""
        if not isinstance(raw, Mapping) or set(raw) != {"revision", "facts", "events"}:
            raise ValueError("snapshot must have revision, facts, and events")
        events = raw["events"]
        if not isinstance(events, list) or len(events) > _MAX_EVENTS:
            raise ValueError("snapshot events must be a bounded list")
        reconstructed = cls()
        for record in events:
            reconstructed.ingest(record)
        try:
            expected = json.dumps(
                reconstructed.snapshot(), allow_nan=False, sort_keys=True, separators=(",", ":")
            )
            actual = json.dumps(raw, allow_nan=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("snapshot must be JSON-compatible") from exc
        if expected != actual:
            raise ValueError("snapshot conflicts with its event history")
        return reconstructed


__all__ = ["WorldState"]
