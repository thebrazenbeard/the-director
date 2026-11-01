"""Bounded classical mission generation from a goal and declared capabilities.

This is an *analytical* planner, not a general-purpose autonomous executor. It
does not run missions, authenticate an agent, send messages, query a provider,
or confer permission to change anything outside this process. A capability's
``expected_delta`` is an unverified modelling assumption, never a forecast of
future events. Observations and sender identities are declared input data.

Inputs deliberately have a small, explicit shape:

* goal: ``{goal_id, objective, metric, target_value, direction}``, with
  ``direction`` one of ``INCREASE`` or ``DECREASE``.
* capability: ``{capability_id, agent_id, metric, expected_delta, cost_units}``
  plus an optional short ``description``. ``cost_units`` must be zero.
* world_snapshot: ``{revision, facts, ...}`` from ``WorldState.snapshot()``;
  ``facts[metric]`` supplies ``{value, status}``.

A missing or disputed observation generates requests for *analysis* only. It
cannot silently turn a hypothesis into a fact. When an observed metric reaches
the target, the planner produces no further missions for that goal.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_GOAL_KEYS = frozenset({"goal_id", "objective", "metric", "target_value", "direction"})
_CAPABILITY_KEYS = frozenset({
    "capability_id", "agent_id", "metric", "expected_delta", "cost_units"
})
_MAX_CAPABILITIES = 128
_MAX_METRIC_ABS = 1_000_000_000


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError(f"{label} must be a bounded identifier")
    return value


def _finite_number(value: object, label: str) -> float:
    # bool is a subclass of int, but a boolean must not count as evidence of
    # progress or an estimated effect.
    if type(value) not in (int, float):
        raise ValueError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be a bounded finite number") from exc
    if not math.isfinite(number) or abs(number) > _MAX_METRIC_ABS:
        raise ValueError(f"{label} must be a bounded finite number")
    return number


def _goal(raw: object) -> dict[str, object]:
    if not isinstance(raw, Mapping) or set(raw) != _GOAL_KEYS:
        raise ValueError("goal must contain exactly the documented fields")
    objective = raw["objective"]
    if (
        not isinstance(objective, str)
        or not 1 <= len(objective) <= 1000
        or not objective.strip()
        or not objective.isprintable()
    ):
        raise ValueError("objective must be bounded printable text")
    direction = raw["direction"]
    if direction not in ("INCREASE", "DECREASE"):
        raise ValueError("direction must be INCREASE or DECREASE")
    return {
        "goal_id": _identifier(raw["goal_id"], "goal_id"),
        "objective": objective,
        "metric": _identifier(raw["metric"], "metric"),
        "target_value": _finite_number(raw["target_value"], "target_value"),
        "direction": direction,
    }


def _capabilities(raw: object) -> list[dict[str, object]]:
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise ValueError("capabilities must be a bounded list")
    if len(raw) > _MAX_CAPABILITIES:
        raise ValueError("capabilities must be a bounded list")
    seen: set[str] = set()
    result: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("each capability must be a mapping")
        if set(item) not in (_CAPABILITY_KEYS, _CAPABILITY_KEYS | {"description"}):
            raise ValueError("capabilities must contain only the documented fields")
        capability_id = _identifier(item["capability_id"], "capability_id")
        if capability_id in seen:
            raise ValueError("duplicate capability_id")
        seen.add(capability_id)
        description = item.get("description", "")
        if (
            not isinstance(description, str)
            or len(description) > 250
            or not description.isprintable()
        ):
            raise ValueError("description must be bounded printable text")
        if type(item["cost_units"]) is not int or item["cost_units"] != 0:
            raise ValueError("only zero-cost analytical capabilities are supported")
        result.append({
            "capability_id": capability_id,
            "agent_id": _identifier(item["agent_id"], "agent_id"),
            "metric": _identifier(item["metric"], "metric"),
            "expected_delta": _finite_number(item["expected_delta"], "expected_delta"),
            "description": description,
        })
    return result


def _world_fact(raw: object, metric: str) -> tuple[int, str, float | None]:
    if not isinstance(raw, Mapping):
        raise ValueError("world_snapshot must be a mapping")
    revision = raw.get("revision")
    if type(revision) is not int or not 0 <= revision <= 2**31 - 1:
        raise ValueError("world_snapshot.revision must be a bounded nonnegative integer")
    facts = raw.get("facts")
    if not isinstance(facts, Mapping) or len(facts) > 1024:
        raise ValueError("world_snapshot.facts must be a bounded mapping")
    fact = facts.get(metric)
    if fact is None:
        return revision, "UNKNOWN", None
    if not isinstance(fact, Mapping):
        raise ValueError("world metric fact must be a mapping")
    status = fact.get("status")
    if status not in ("OBSERVED", "CONTESTED", "INFERRED", "HYPOTHETICAL", "UNKNOWN"):
        raise ValueError("world metric has unsupported status")
    if status == "OBSERVED":
        return revision, status, _finite_number(fact.get("value"), "observed metric value")
    # Even a numerically plausible hypothetical is never promoted to observed.
    return revision, status, None


def _mission_id(goal: Mapping[str, object], capability: Mapping[str, object], revision: int, kind: str) -> str:
    # Bind every material declared input, not just human-readable IDs. A goal
    # target or roster revision must not accidentally alias an older mission.
    serial = json.dumps(
        [goal, capability, revision, kind],
        sort_keys=True, separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return "dr-" + hashlib.sha256(serial).hexdigest()[:24]


def generate_candidates(
    goal: Mapping[str, object],
    capabilities: Sequence[Mapping[str, object]],
    world_snapshot: Mapping[str, object],
    *,
    max_candidates: int = 8,
) -> list[dict[str, object]]:
    """Generate ranked, advisory analytical missions with no external effects.

    The caller supplies one objective, a bounded capability catalog, and an
    observation snapshot. Ranking is deterministic but it is **not** evidence
    of attainable outcomes: deltas and rankings are estimates. No proposed
    mission constitutes an approval or an instruction to execute operations.
    """
    if type(max_candidates) is not int or not 1 <= max_candidates <= 32:
        raise ValueError("max_candidates must be an integer from 1 through 32")
    parsed_goal = _goal(goal)
    parsed_capabilities = _capabilities(capabilities)
    goal_id = parsed_goal["goal_id"]
    metric = parsed_goal["metric"]
    direction = parsed_goal["direction"]
    target = parsed_goal["target_value"]
    revision, observation_status, current = _world_fact(world_snapshot, str(metric))

    # A declared observation can cause abstention; lack of evidence cannot.
    if current is not None and (
        (direction == "INCREASE" and current >= target)
        or (direction == "DECREASE" and current <= target)
    ):
        return []

    matching = [cap for cap in parsed_capabilities if cap["metric"] == metric]
    candidates: list[tuple[tuple[float, float, str], dict[str, object]]] = []
    gap = abs(target - current) if current is not None else None
    for cap in matching:
        delta = cap["expected_delta"]
        cap_id = str(cap["capability_id"])
        if current is None:
            kind = "VERIFY" if observation_status == "CONTESTED" else "OBSERVE"
            projected: float | None = None
            progress: float | None = None
            # No numerical or directional assumption is possible without an
            # observed baseline. Different capabilities remain alternatives.
            rank = (0.0, 0.0, cap_id)
            rationale = (
                "Verify conflicting declared observations before planning"
                if kind == "VERIFY"
                else "Gather an observation before making a numerical plan"
            )
        else:
            # A claimed effect in the wrong direction cannot advance the
            # objective. Zero-delta capabilities can still analyze options.
            signed_progress = delta if direction == "INCREASE" else -delta
            if signed_progress < 0:
                continue
            kind = "PLAN" if signed_progress > 0 else "ANALYZE"
            projected = current + delta
            if not math.isfinite(projected) or abs(projected) > _MAX_METRIC_ABS:
                continue
            progress = min(gap, signed_progress) if gap is not None else None
            overshoot = max(0.0, signed_progress - gap) if gap is not None else 0.0
            rank = (-(progress or 0.0), overshoot, cap_id)
            rationale = (
                "Compare a modelled change toward the goal against observations"
                if kind == "PLAN"
                else "Analyze alternatives without claiming measurable progress"
            )
        candidate: dict[str, object] = {
            "mission_id": _mission_id(parsed_goal, cap, revision, kind),
            "goal_id": goal_id,
            "plan_revision": revision,
            "assigned_agent_id": cap["agent_id"],
            "capability_id": cap_id,
            "metric": metric,
            "kind": kind,
            "rationale": rationale,
            "observed_value": current,
            "observation_status": observation_status,
            "target_value": target,
            "expected_delta": delta if kind in ("PLAN", "ANALYZE") else None,
            "projected_value": projected,
            "progress_points": progress,
            "model_status": "SIMULATED_UNVERIFIED",
            "input_integrity": "DECLARED_UNVERIFIED",
            "effect_status": "NOT_EXECUTED",
            "requires_human_authorization": True,
        }
        candidates.append((rank, candidate))

    candidates.sort(key=lambda item: item[0])
    return [item[1] for item in candidates[:max_candidates]]
