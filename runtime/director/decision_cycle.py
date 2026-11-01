"""Read-only, bounded decision-cycle assessments for the local Director.

This module joins *declared* evidence, rival predictions, registered analytical
capabilities and dependency readiness. It produces an attributable recommendation,
not an instruction, observation, authenticated result, or permission to execute.
No mailbox, SQLite, network, clock, process, or worker interface exists here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

from .mission_graph import MissionGraph
from .reasoning import LocalReasoner, _parse_hypotheses


SCHEMA = "director.decision-cycle.v1"
# WorldState admits 1,024 events containing up to 512 Unicode characters each.
# ASCII-escaped non-BMP characters can consume 12 bytes per character; the
# snapshot also repeats up to 512 fact values and includes provenance arrays.
# Hypotheses add up to 32x16 bounded predictions and the DAG adds 128 nodes.
# 16 MiB accommodates these valid source limits while remaining bounded.
_MAX_INPUT_BYTES = 16 * 1024 * 1024


def assess_decision_cycle(
    goal: Mapping[str, object],
    capabilities: Sequence[Mapping[str, object]],
    world_snapshot: Mapping[str, object],
    hypotheses: Sequence[Mapping[str, object]],
    *,
    graph_ready: Sequence[str] | None,
    graph_snapshot: Mapping[str, object] | None,
    operator_control: Mapping[str, object],
    quarantined: bool,
) -> dict[str, object]:
    """Return deterministic, no-effect analysis from a consistent caller view.

    A registered capability is only a *possible* analytical route, even when
    graph-ready. ``generate_candidates`` still limits coordinator dispatch to
    its configured goal metric; this method never extends that contract.
    """
    reasoner = LocalReasoner()
    normalized_hypotheses = _parse_hypotheses(hypotheses)
    if not isinstance(capabilities, (list, tuple)) or len(capabilities) > 32:
        raise ValueError("capabilities must be a bounded registered sequence")
    options: list[dict[str, object]] = []
    goal_options: list[dict[str, object]] = []
    capability_catalog: list[dict[str, object]] = []
    registered: dict[str, list[str]] = {}
    for capability in capabilities:
        if not isinstance(capability, Mapping):
            raise ValueError("registered capability must be a mapping")
        cap_id = capability["capability_id"]
        metric = capability["metric"]
        options.append({"option_id": cap_id,
                        "expected_delta": capability["expected_delta"],
                        "cost_units": capability["cost_units"],
                        "kind": "ANALYZE"})
        capability_catalog.append(dict(capability))
        if metric == goal.get("metric"):
            goal_options.append(options[-1])
        registered.setdefault(str(metric), []).append(str(cap_id))
    options.sort(key=lambda option: str(option["option_id"]))
    goal_options.sort(key=lambda option: str(option["option_id"]))
    capability_catalog.sort(key=lambda capability: str(capability["capability_id"]))
    for ids in registered.values():
        ids.sort()

    # A capability for `signal` cannot claim to advance a `quality` goal.
    # It may still be a configured probe for a rival hypothesis below.
    goal_assessment = reasoner.reason(goal, world_snapshot, goal_options)
    hypothesis_assessment = reasoner.suggest_discriminating_observations(
        world_snapshot, normalized_hypotheses)
    ready_set = set(graph_ready) if graph_ready is not None else set()
    if graph_ready is not None and (len(ready_set) != len(graph_ready)
                                    or not ready_set <= set(option["option_id"] for option in options)):
        raise ValueError("graph readiness must reference unique registered capabilities")
    if (graph_ready is None) != (graph_snapshot is None):
        raise ValueError("graph readiness and graph snapshot must both be configured or absent")
    if graph_snapshot is not None:
        if not isinstance(graph_snapshot, Mapping) or not isinstance(graph_snapshot.get("graph"), Mapping):
            raise ValueError("graph snapshot must contain a declared mission graph")
        declaration = graph_snapshot["graph"]
        validated_graph = MissionGraph(declaration["nodes"], declaration["resource_capacities"])
        expected_ready = {node["mission_id"] for node in validated_graph.ready(
            completed=graph_snapshot["completed"], in_progress=graph_snapshot["active"],
            failed_or_cancelled=graph_snapshot["invalidated"],
            unavailable_resources=graph_snapshot["unavailable_resources"],
            now_tick=graph_snapshot["logical_tick"],
        )}
        if expected_ready != ready_set:
            raise ValueError("graph readiness mismatches the declared snapshot")

    probes: list[dict[str, object]] = []
    for proposed in hypothesis_assessment["observations"]:
        metric = proposed["metric"]
        registered_ids = registered.get(metric, [])
        ready_ids = sorted(ready_set.intersection(registered_ids))
        if not registered_ids or graph_ready is None:
            mapping_status = "OPERATOR_CONFIGURATION_REQUIRED"
        elif not ready_ids:
            mapping_status = "DAG_BLOCKED"
        else:
            mapping_status = "REGISTERED_DAG_READY"
        probes.append({**proposed,
                       "registered_capability_ids": list(registered_ids),
                       "graph_ready_capability_ids": ready_ids,
                       "mapping_status": mapping_status,
                       "dispatch_status": "ADVISORY_ONLY_NOT_ISSUED"})

    plan_options = [{"capability_id": option["option_id"],
                     "graph_status": ("GRAPH_UNCONFIGURED" if graph_ready is None
                                      else "DAG_READY" if option["option_id"] in ready_set
                                      else "DAG_BLOCKED")}
                    for option in options]
    if quarantined:
        status = "QUARANTINED_ANALYSIS_ONLY"
    elif operator_control.get("paused") or operator_control.get("stop_requested"):
        status = "OPERATOR_CONTROLLED_ANALYSIS_ONLY"
    elif not probes:
        status = "NO_SUPPORTED_MEASUREMENT"
    elif any(p["mapping_status"] == "REGISTERED_DAG_READY" for p in probes):
        status = "ADVISORY_PROBES_MAPPED"
    else:
        status = "OPERATOR_CONFIGURATION_REQUIRED"

    # Full *input* binding, not merely a digest of the chosen proposal. A
    # changed observation, graph, operator control or competing hypothesis
    # invalidates an older assessment, even if its recommendation is unchanged.
    normalized_input = {
        "schema": SCHEMA, "goal": dict(goal), "capabilities": capability_catalog,
        "world_snapshot": dict(world_snapshot),
        "hypotheses": normalized_hypotheses,
        "graph_snapshot": None if graph_snapshot is None else dict(graph_snapshot),
        "graph_ready": None if graph_ready is None else sorted(ready_set),
        "operator_control": dict(operator_control), "quarantined": quarantined,
    }
    try:
        encoded = json.dumps(normalized_input, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True, allow_nan=False).encode("ascii")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("decision-cycle input must be bounded JSON data") from exc
    if len(encoded) > _MAX_INPUT_BYTES:
        raise ValueError("decision-cycle input exceeds the local bound")
    return {"schema": SCHEMA, "input_sha256": hashlib.sha256(encoded).hexdigest(),
            "world_revision": goal_assessment["world_revision"],
            "goal_id": goal_assessment["goal_id"], "status": status,
            "goal_assessment": goal_assessment,
            "hypothesis_assessment": hypothesis_assessment,
            "probe_proposals": probes, "plan_options": plan_options,
            "graph_status": "CONFIGURED" if graph_ready is not None else "UNCONFIGURED",
            "operator_control": dict(operator_control),
            "recovery_quarantined": quarantined,
            "source_authentication": "NOT_ESTABLISHED",
            "authorization": "NOT_GRANTED", "effect_status": "NOT_EXECUTED"}


__all__ = ["SCHEMA", "assess_decision_cycle"]
