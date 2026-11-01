"""Pure, zero-cost dependency/resource model for analytical Director missions.

This is a *planning* graph, not an executor or a durable task scheduler. The
caller declares candidate work and its prerequisites, logical deadlines, and
local resource capacity. Results are advisory and deterministic; no worker is
launched, no channel opened, and no cancellation transmitted. Declared state
is not authenticated and never confers effect authority.

``now_tick`` is an operator-supplied logical time index, **not** a trusted
clock. ``completed`` is an external claim, **not** verified achievement. A
future coordinator must independently validate both before taking protected
action or marking an objective complete.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_REQUIRED = frozenset({"mission_id", "kind", "depends_on", "resources", "priority", "cost_units"})
_ALLOWED = _REQUIRED | {"deadline_tick"}
_KINDS = frozenset({"OBSERVE", "VERIFY", "ANALYZE", "PLAN"})
_MAX_NODES = 128
_MAX_RESOURCES = 32
_MAX_CAPACITY = 1024
_MAX_DEADLINE_TICK = 1_000_000_000


def _identifier(raw: object, label: str) -> str:
    if not isinstance(raw, str) or _ID.fullmatch(raw) is None:
        raise ValueError(f"{label} must be a bounded identifier")
    return raw


def _bounded_int(raw: object, label: str, lower: int, upper: int) -> int:
    if type(raw) is not int or not lower <= raw <= upper:
        raise ValueError(f"{label} must be an integer in {lower}..{upper}")
    return raw


def _resources(raw: object, *, caps: Mapping[str, int] | None = None) -> dict[str, int]:
    if not isinstance(raw, Mapping) or len(raw) > _MAX_RESOURCES:
        raise ValueError("resources must be a bounded mapping")
    result: dict[str, int] = {}
    for key, amount in raw.items():
        name = _identifier(key, "resource name")
        result[name] = _bounded_int(amount, f"resource {name}", 0 if caps is None else 1, _MAX_CAPACITY)
        if caps is not None:
            if name not in caps:
                raise ValueError("mission requests an undeclared resource")
            if result[name] > caps[name]:
                raise ValueError("mission resource request exceeds declared capacity")
    return dict(sorted(result.items()))


def _id_set(raw: object, *, known: set[str], label: str) -> set[str]:
    if isinstance(raw, (str, bytes, Mapping)) or not isinstance(raw, Collection):
        raise ValueError(f"{label} must be a collection of identifiers")
    if len(raw) > _MAX_NODES:
        raise ValueError(f"{label} exceeds mission limit")
    result = {_identifier(value, label) for value in raw}
    if not result <= known:
        raise ValueError(f"{label} includes unknown identifiers")
    return result


class MissionGraph:
    """Validated, immutable in-memory DAG of non-effectful analytical work.

    Node schema: ``{mission_id, kind, depends_on: list[str], resources:
    dict[str,int], priority: int, cost_units: 0, deadline_tick?: int}``.

    Resource caps are integer slots (not money), including zero if a facility
    is disabled. ``available_resources`` overrides total capacity **before**
    subtracting in-progress reservations; it does not mean free slots after
    those reservations. ``ready`` greedily reserves the resulting capacity in
    priority/deadline order; this is only a scheduling *recommendation*.
    """

    def __init__(self, nodes: Sequence[Mapping[str, object]], resource_capacities: Mapping[str, int]):
        if isinstance(nodes, (str, bytes)) or not isinstance(nodes, Sequence) or not 1 <= len(nodes) <= _MAX_NODES:
            raise ValueError("nodes must be a sequence containing 1..128 analytical missions")
        self._caps = _resources(resource_capacities)
        self._nodes: dict[str, dict[str, object]] = {}
        for raw in nodes:
            if not isinstance(raw, Mapping) or not _REQUIRED <= set(raw) or not set(raw) <= _ALLOWED:
                raise ValueError("node must contain exactly the documented fields")
            mission_id = _identifier(raw["mission_id"], "mission_id")
            if mission_id in self._nodes:
                raise ValueError("duplicate mission_id")
            kind = raw["kind"]
            if not isinstance(kind, str) or kind not in _KINDS:
                raise ValueError("mission kind must be analytical")
            dependencies = raw["depends_on"]
            if isinstance(dependencies, (str, bytes)) or not isinstance(dependencies, (list, tuple)) or len(dependencies) > _MAX_NODES:
                raise ValueError("depends_on must be a bounded list")
            dependency_ids = [_identifier(dep, "dependency") for dep in dependencies]
            if len(set(dependency_ids)) != len(dependency_ids) or mission_id in dependency_ids:
                raise ValueError("duplicate or self dependency")
            monetary_cost = raw["cost_units"]
            if type(monetary_cost) is not int or monetary_cost != 0:
                raise ValueError("no monetary-cost missions are permitted")
            row: dict[str, object] = {
                "mission_id": mission_id,
                "kind": kind,
                "depends_on": sorted(dependency_ids),
                "resources": _resources(raw["resources"], caps=self._caps),
                "priority": _bounded_int(raw["priority"], "priority", 0, 100),
                "cost_units": 0,
            }
            if "deadline_tick" in raw:
                row["deadline_tick"] = _bounded_int(raw["deadline_tick"], "deadline_tick", 0, _MAX_DEADLINE_TICK)
            self._nodes[mission_id] = row

        known = set(self._nodes)
        for node in self._nodes.values():
            if not set(node["depends_on"]) <= known:
                raise ValueError("unknown mission dependency")
        # Deterministic DFS rejects cycles even if they span multiple layers.
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(mission_id: str) -> None:
            if mission_id in visiting:
                raise ValueError("cyclic mission dependencies")
            if mission_id in visited:
                return
            visiting.add(mission_id)
            for parent in self._nodes[mission_id]["depends_on"]:
                visit(parent)
            visiting.remove(mission_id)
            visited.add(mission_id)

        for mission_id in sorted(known):
            visit(mission_id)

    def snapshot(self) -> dict[str, object]:
        """JSON-compatible defensive copy; does not include claim authenticity."""
        return {
            "nodes": [self._copy_node(self._nodes[node]) for node in sorted(self._nodes)],
            "resource_capacities": dict(self._caps),
            "effect_status": "NOT_EXECUTED",
        }

    @staticmethod
    def _copy_node(node: Mapping[str, object]) -> dict[str, object]:
        return {
            **node,
            "depends_on": list(node["depends_on"]),
            "resources": dict(node["resources"]),
        }

    def _unavailable(self, raw: object) -> set[str]:
        return _id_set(raw, known=set(self._caps), label="unavailable_resources")

    def cascade_blocked(
        self, *, failed_or_cancelled: Collection[str] = (),
        unavailable_resources: Collection[str] = (), now_tick: int | None = None,
        completed: Collection[str] = (),
    ) -> dict[str, object]:
        """Suggest direct and transitive cancel/hold; perform **no** cancel."""
        failed = _id_set(failed_or_cancelled, known=set(self._nodes), label="failed_or_cancelled")
        done = _id_set(completed, known=set(self._nodes), label="completed")
        if failed & done:
            raise ValueError("completed missions cannot also be failed or cancelled")
        outage = self._unavailable(unavailable_resources)
        if now_tick is not None:
            _bounded_int(now_tick, "now_tick", 0, _MAX_DEADLINE_TICK)
        direct: set[str] = set(failed)
        reasons: dict[str, list[str]] = {mission_id: ["FAILED_OR_CANCELLED"] for mission_id in failed}
        for mission_id, node in self._nodes.items():
            if mission_id in done:
                continue
            for resource in sorted(outage.intersection(node["resources"])):
                direct.add(mission_id)
                reasons.setdefault(mission_id, []).append(f"RESOURCE_UNAVAILABLE:{resource}")
            if now_tick is not None and "deadline_tick" in node and now_tick > node["deadline_tick"]:
                direct.add(mission_id)
                reasons.setdefault(mission_id, []).append("DEADLINE_EXPIRED")

        blocked = set(direct)
        changed = True
        while changed:
            changed = False
            for mission_id, node in self._nodes.items():
                if mission_id in blocked or mission_id in done:
                    continue
                failed_prerequisites = sorted(set(node["depends_on"]).intersection(blocked))
                if failed_prerequisites:
                    blocked.add(mission_id)
                    reasons[mission_id] = [f"DEPENDENCY_BLOCKED:{parent}" for parent in failed_prerequisites]
                    changed = True

        return {
            "directly_blocked": sorted(direct),
            "downstream_blocked": sorted(blocked - direct),
            "recommended_cancellations": sorted(blocked),
            "reasons": {name: sorted(reasons[name]) for name in sorted(reasons)},
            "effect_status": "NOT_EXECUTED",
            "authorization": "NOT_GRANTED",
        }

    def ready(
        self, *, completed: Collection[str] = (), in_progress: Collection[str] = (),
        failed_or_cancelled: Collection[str] = (), available_resources: Mapping[str, int] | None = None,
        unavailable_resources: Collection[str] = (), now_tick: int = 0,
    ) -> list[dict[str, object]]:
        """Return only concurrently admissible analytical tasks, no effects."""
        _bounded_int(now_tick, "now_tick", 0, _MAX_DEADLINE_TICK)
        known = set(self._nodes)
        done = _id_set(completed, known=known, label="completed")
        active = _id_set(in_progress, known=known, label="in_progress")
        failed = _id_set(failed_or_cancelled, known=known, label="failed_or_cancelled")
        if done & active or done & failed or active & failed:
            raise ValueError("mission completion/in-progress/failure sets must be disjoint")
        outage = self._unavailable(unavailable_resources)
        slots = dict(self._caps)
        if available_resources is not None:
            if not isinstance(available_resources, Mapping) or not set(available_resources) <= set(self._caps):
                raise ValueError("available_resources includes undeclared resources")
            for name, amount in available_resources.items():
                available = _bounded_int(amount, f"available {name}", 0, _MAX_CAPACITY)
                if available > self._caps[name]:
                    raise ValueError("available resources exceed configured capacity")
                slots[name] = available
        for name in outage:
            slots[name] = 0
        for mission_id in active:
            for resource, amount in self._nodes[mission_id]["resources"].items():
                slots[resource] = max(0, slots[resource] - amount)
        failures = self.cascade_blocked(
            failed_or_cancelled=failed,
            # Temporary capacity exhaustion (including active reservations)
            # means WAIT, not a permanent outage/cancellation cascade.
            unavailable_resources=outage,
            now_tick=now_tick,
            completed=done,
        )
        blocked = set(failures["recommended_cancellations"])
        candidates = [
            node for node in self._nodes.values()
            if node["mission_id"] not in done | active | blocked
            and set(node["depends_on"]) <= done
        ]
        candidates.sort(key=lambda node: (
            node.get("deadline_tick", _MAX_DEADLINE_TICK + 1),
            -node["priority"],
            node["mission_id"],
        ))

        selected: list[dict[str, object]] = []
        for node in candidates:
            if any(slots[resource] < amount for resource, amount in node["resources"].items()):
                continue
            for resource, amount in node["resources"].items():
                slots[resource] -= amount
            proposal = self._copy_node(node)
            proposal.update({
                "effect_status": "NOT_EXECUTED",
                "effect_authority": "NOT_GRANTED",
                "requires_operator_authorization_for_effects": True,
                "source_status": "DECLARED_UNVERIFIED",
            })
            selected.append(proposal)
        return selected


__all__ = ["MissionGraph"]
