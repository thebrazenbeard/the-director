"""Fail-closed stopping policy for a local, advisory Director coordination loop.

The policy does not establish whether a claim is true. Evidence identifiers,
``verified`` flags, and source labels in untrusted messages are *claims*. A
success decision additionally requires confirmation of the *same evidence ID*
in trusted local ``operator_control`` state. The caller is responsible for
keeping that state separate from worker replies and network messages.

No status returned here authorizes physical, network, or host-side effects.
There is no clock, I/O, model inference, or executable task dispatch.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Mapping


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}\Z")
_MAX_FACTS = 512
_MAX_CONFIRMED = 128


class StopStatus(str, Enum):
    """Descriptive advisory state, never an execution permission."""

    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    OBJECTIVE_MET = "OBJECTIVE_MET"
    UNRESOLVED = "UNRESOLVED"
    EXHAUSTED = "EXHAUSTED"


@dataclass(frozen=True)
class StopDecision:
    status: StopStatus
    reason: str
    evidence_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Return a stable, JSON-serializable advisory receipt."""

        return {
            "status": self.status.value,
            "reason": self.reason,
            "evidence_ids": list(self.evidence_ids),
            "authorizes_effects": False,
        }


def _identifier(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _scalar(value: object) -> bool:
    """Permit exact primitive JSON values, never truthy coercion or NaN."""

    if value is None or type(value) in (bool, int, str):
        return value is None or not isinstance(value, str) or len(value) <= 512
    return type(value) is float and math.isfinite(value)


def _same_value(left: object, right: object) -> bool:
    # Numeric equality alone would mistake ``True`` for observed integer 1.
    return type(left) is type(right) and left == right


def _ids(value: object, *, limit: int) -> set[str] | None:
    if not isinstance(value, (list, tuple)) or len(value) > limit:
        return None
    if not all(_identifier(item) for item in value):
        return None
    result = set(value)
    return result if len(result) == len(value) else None


def _pause_reason(operator_control: object) -> str | None:
    if not isinstance(operator_control, Mapping):
        return "INVALID_OPERATOR_CONTROL"
    # Unrecognized types (e.g. 'false' as a string) must never start a run.
    if type(operator_control.get("paused", False)) is not bool:
        return "INVALID_OPERATOR_CONTROL"
    if type(operator_control.get("stop_requested", False)) is not bool:
        return "INVALID_OPERATOR_CONTROL"
    if operator_control.get("stop_requested"):
        return "OPERATOR_STOP_REQUESTED"
    if operator_control.get("paused"):
        return "OPERATOR_PAUSE_REQUESTED"
    return None


def evaluate_stop(
    goal_state: Mapping[str, object],
    observations: Mapping[str, object],
    operator_control: Mapping[str, object],
) -> StopDecision:
    """Evaluate whether a bounded local goal loop should pause or continue.

    Minimum goal: ``success_metric`` (identifier), ``success_value`` (JSON
    primitive), optional ``remaining_attempts`` (nonnegative integer).
    ``goal_id`` is optional to this pure policy.

    Observations: ``facts`` is a bounded sequence of mappings with
    ``evidence_id``, ``metric``, ``value``, ``evidence_class='OBSERVATION'``,
    ``status='OBSERVED'``, and ``verified=True``. ``contested_metrics`` may
    name measurements whose truth is disputed. Only matching, uncontested,
    unique confirmed evidence can support ``OBJECTIVE_MET``.

    Local operator control: ``paused``, ``stop_requested`` and
    ``confirmed_success_evidence_ids`` (bounded list of verified evidence IDs).
    This input must come from trusted local state, **never** a task response.

    Explicit pause has highest priority. With no trusted evidence of success,
    zero attempts returns EXHAUSTED, positive attempts RUNNING, and unknown
    attempt budget UNRESOLVED. All outputs are advisory and effect-free.
    """

    pause = _pause_reason(operator_control)
    if pause is not None:
        return StopDecision(StopStatus.PAUSED, pause)

    if not isinstance(goal_state, Mapping) or not isinstance(observations, Mapping):
        return StopDecision(StopStatus.UNRESOLVED, "INVALID_POLICY_INPUT")

    metric = goal_state.get("success_metric")
    expected = goal_state.get("success_value")
    if not _identifier(metric) or "success_value" not in goal_state or not _scalar(expected):
        return StopDecision(StopStatus.UNRESOLVED, "INVALID_GOAL_SUCCESS_CONDITION")

    attempt_budget = goal_state.get("remaining_attempts")
    if attempt_budget is not None and (
        type(attempt_budget) is not int or not 0 <= attempt_budget <= 2**31 - 1
    ):
        return StopDecision(StopStatus.UNRESOLVED, "INVALID_ATTEMPT_BUDGET")

    confirmed = _ids(
        operator_control.get("confirmed_success_evidence_ids", []), limit=_MAX_CONFIRMED
    )
    if confirmed is None:
        return StopDecision(StopStatus.UNRESOLVED, "INVALID_SUCCESS_CONFIRMATIONS")

    contested = _ids(observations.get("contested_metrics", []), limit=_MAX_FACTS)
    if contested is None:
        return StopDecision(StopStatus.UNRESOLVED, "INVALID_CONTESTED_METRICS")

    facts = observations.get("facts", [])
    if not isinstance(facts, (list, tuple)) or len(facts) > _MAX_FACTS:
        return StopDecision(StopStatus.UNRESOLVED, "INVALID_OBSERVATION_LIST")
    seen: set[str] = set()
    valid_observed: list[Mapping[str, object]] = []
    for fact in facts:
        if not isinstance(fact, Mapping):
            return StopDecision(StopStatus.UNRESOLVED, "INVALID_OBSERVATION")
        evidence_id = fact.get("evidence_id")
        fact_metric = fact.get("metric")
        if (
            not _identifier(evidence_id)
            or not _identifier(fact_metric)
            or "value" not in fact
            or not _scalar(fact["value"])
        ):
            return StopDecision(StopStatus.UNRESOLVED, "INVALID_OBSERVATION")
        if evidence_id in seen:
            return StopDecision(StopStatus.UNRESOLVED, "DUPLICATE_EVIDENCE_ID")
        seen.add(evidence_id)
        if fact.get("evidence_class") == "OBSERVATION" and fact.get("status") == "OBSERVED":
            valid_observed.append(fact)

    if metric in contested:
        return StopDecision(StopStatus.UNRESOLVED, "CONTESTED_SUCCESS_METRIC")

    # Multiple observed values for the same metric mean an unresolved conflict,
    # even if one of their IDs was operator-confirmed. A projection, inference,
    # or agent assertion cannot count toward this consistency comparison.
    related = [fact for fact in valid_observed if fact["metric"] == metric]
    if any(not _same_value(fact["value"], related[0]["value"]) for fact in related[1:]):
        return StopDecision(StopStatus.UNRESOLVED, "CONFLICTING_OBSERVATIONS")

    matching = tuple(sorted(
        fact["evidence_id"]
        for fact in related
        if _same_value(fact["value"], expected)
        and fact.get("verified") is True
        and fact["evidence_id"] in confirmed
    ))
    if matching:
        return StopDecision(StopStatus.OBJECTIVE_MET, "OPERATOR_CONFIRMED_OBSERVED_SUCCESS", matching)
    if attempt_budget == 0:
        return StopDecision(StopStatus.EXHAUSTED, "NO_REMAINING_ATTEMPTS")
    if attempt_budget is None:
        return StopDecision(StopStatus.UNRESOLVED, "ATTEMPT_BUDGET_UNSPECIFIED")
    return StopDecision(StopStatus.RUNNING, "SUCCESS_NOT_YET_VERIFIED")
