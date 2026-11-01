"""Fixed, data-only worker process for :mod:`runtime.director.agent_runtime`.

This is intentionally a **script**, launched via ``python -I -S -u``. It
imports only the Python standard library and accepts exactly five fixed JSON
operations: INIT, STATUS, OBSERVE, THINK and STOP. It never executes incoming
Python, opens a network interface, or interprets capsule memory as instructions.

Process separation is *not* an OS security sandbox against malicious code. No
arbitrary code is admitted in the first place. Do not generalize this worker to
run plugins or executable payloads without a separately qualified OS boundary.
"""

from __future__ import annotations

import json
import math
import re
import sys


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_MAX_WIRE = 32768


def _id(value: object) -> str:
    if type(value) is not str or not _ID.fullmatch(value):
        raise ValueError("invalid bounded identifier")
    return value


def _number(value: object) -> float | int:
    if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 10**12:
        raise ValueError("value must be a bounded finite number")
    return value


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, val in items:
        if key in result:
            raise ValueError("duplicate JSON property")
        result[key] = val
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("non-JSON number: " + value)


def _read() -> dict[str, object] | None:
    line = sys.stdin.buffer.readline(_MAX_WIRE + 1)
    if not line:
        return None
    if len(line) > _MAX_WIRE or not line.endswith(b"\n"):
        raise ValueError("protocol line exceeds limit")
    try:
        raw = json.loads(line.decode("ascii"), object_pairs_hook=_pairs,
                         parse_constant=_invalid_constant)
    except (UnicodeError, ValueError, TypeError) as exc:
        raise ValueError("invalid JSON input") from exc
    if type(raw) is not dict:
        raise ValueError("operation must be a JSON object")
    return raw


def _send(data: dict[str, object]) -> None:
    line = json.dumps(data, sort_keys=True, separators=(",", ":"),
                      allow_nan=False, ensure_ascii=True).encode("ascii")
    if len(line) > _MAX_WIRE - 1:
        raise ValueError("response is too long")
    sys.stdout.buffer.write(line + b"\n")
    sys.stdout.buffer.flush()


def _only(message: dict[str, object], keys: set[str]) -> None:
    if set(message) != keys:
        raise ValueError("operation schema contains unexpected fields")


def _init(message: dict[str, object]) -> dict[str, object]:
    _only(message, {"op", "record", "max_steps"})
    record = message["record"]
    max_steps = message["max_steps"]
    if type(record) is not dict or type(max_steps) is not int or not 1 <= max_steps <= 256:
        raise ValueError("invalid software-state record or step ceiling")
    expected = {"new_instance_id", "agent_id", "restored_from_instance_id", "capsule_id",
                "target_domain", "subject_kind", "state", "memory_manifest", "authority",
                "effects", "continuity_claim"}
    if set(record) != expected:
        raise ValueError("unexpected restored record fields")
    if (record["subject_kind"] != "SOFTWARE_AGENT" or record["authority"] != "NOT_GRANTED"
            or record["effects"] != "NOT_EXECUTED"
            or record["continuity_claim"] != "INFORMATION_STATE_COPY_ONLY"):
        raise ValueError("restored state must be data-only, not authorized")
    for key in ("new_instance_id", "agent_id", "restored_from_instance_id", "capsule_id", "target_domain"):
        _id(record[key])
    if record["new_instance_id"] == record["restored_from_instance_id"]:
        raise ValueError("source identity cannot be overwritten")
    state = record["state"]
    if type(state) is not dict or set(state) != {"goal", "memories"}:
        raise ValueError("restored state must be goal and memories only")
    goal = state["goal"]
    memories = state["memories"]
    if type(goal) is not dict or set(goal) != {"goal_id", "objective", "metric", "target_value", "direction"}:
        raise ValueError("invalid goal schema")
    _id(goal["goal_id"])
    _id(goal["metric"])
    if type(goal["objective"]) is not str or not 1 <= len(goal["objective"]) <= 512:
        raise ValueError("invalid objective text")
    _number(goal["target_value"])
    if goal["direction"] not in ("INCREASE", "DECREASE"):
        raise ValueError("invalid goal direction")
    if type(memories) is not list or len(memories) > 32:
        raise ValueError("invalid memory count")
    for memory in memories:
        if (type(memory) is not dict or set(memory) != {"memory_id", "summary"}
                or type(memory["summary"]) is not str or not 1 <= len(memory["summary"]) <= 512):
            raise ValueError("invalid memory schema")
        _id(memory["memory_id"])
    manifest = record["memory_manifest"]
    if (type(manifest) is not dict or set(manifest) != {"input_count", "transferred_count", "omitted_count"}
            or any(type(manifest[key]) is not int or not 0 <= manifest[key] <= 32 for key in manifest)
            or manifest["transferred_count"] != len(memories)
            or manifest["input_count"] != manifest["transferred_count"] + manifest["omitted_count"]):
        raise ValueError("invalid memory manifest")
    return {"goal": dict(goal), "instance_id": record["new_instance_id"],
            "agent_id": record["agent_id"], "capsule_id": record["capsule_id"],
            "restored_from_instance_id": record["restored_from_instance_id"],
            "target_domain": record["target_domain"],
            "memories": [dict(memory) for memory in memories],
            "memory_count": len(memories), "max_steps": max_steps,
            "seen": {}, "latest": None, "steps": 0}


def _status(state: dict[str, object]) -> dict[str, object]:
    return {
        "state": "RUNNING", "instance_id": state["instance_id"],
        "agent_id": state["agent_id"], "capsule_id": state["capsule_id"],
        "restored_from_instance_id": state["restored_from_instance_id"],
        "target_domain": state["target_domain"],
        "goal_id": state["goal"]["goal_id"], "memory_count": state["memory_count"],
        "observation_count": len(state["seen"]),
        "steps_remaining": state["max_steps"] - state["steps"],
        "approval_authenticated": False, "host_effects": "NOT_EXECUTED",
        "execution_mode": "FIXED_DATA_ONLY_PROTOCOL",
    }


def _observe(state: dict[str, object], message: dict[str, object]) -> dict[str, object]:
    _only(message, {"op", "observation_id", "metric", "value"})
    observation_id = _id(message["observation_id"])
    metric = _id(message["metric"])
    value = _number(message["value"])
    if metric != state["goal"]["metric"]:
        raise ValueError("observation metric outside the agent's declared goal")
    prior = state["seen"].get(observation_id)
    if prior is not None:
        if prior != [metric, value] or type(prior[1]) is not type(value):
            raise ValueError("observation identifier already used for different content")
    else:
        if state["steps"] >= state["max_steps"]:
            raise RuntimeError("step budget exhausted")
        state["seen"][observation_id] = [metric, value]
        state["latest"] = [observation_id, value]
        state["steps"] += 1
    return {"observations": len(state["seen"]), "evidence_status": "DECLARED_UNVERIFIED",
            "effects": "NOT_EXECUTED", "instance_id": state["instance_id"]}


def _think(state: dict[str, object], message: dict[str, object]) -> dict[str, object]:
    _only(message, {"op"})
    if state["steps"] >= state["max_steps"]:
        raise RuntimeError("step budget exhausted")
    state["steps"] += 1
    goal = state["goal"]
    latest = state["latest"]
    if latest is None:
        assessment = "NEEDS_DECLARED_OBSERVATION"
        gap = None
        obs_id = None
    else:
        obs_id, observed = latest
        target = goal["target_value"]
        if goal["direction"] == "INCREASE":
            gap = max(0, target - observed)
        else:
            gap = max(0, observed - target)
        assessment = ("MORE_ANALYSIS_REQUIRED" if gap > 0 else
                      "DECLARED_TARGET_REACHED_UNVERIFIED")
    return {"assessment": assessment, "remaining_gap": gap,
            "observation_id": obs_id, "evidence_status": "DECLARED_UNVERIFIED",
            "objective_verified": False, "authority": "NOT_GRANTED",
            "effects": "NOT_EXECUTED", "instance_id": state["instance_id"],
            "relevant_memory_ids": sorted(memory["memory_id"] for memory in state["memories"]
                                          if goal["metric"].casefold() in memory["summary"].casefold())}


def main() -> None:
    try:
        first = _read()
        if first is None or first.get("op") != "INIT":
            raise ValueError("first message must be INIT")
        state = _init(first)
        _send({"op": "READY", **_status(state)})
    except (ValueError, TypeError, KeyError) as exc:
        _send({"op": "ERROR", "error_code": "INIT_REJECTED", "message": str(exc)[:160]})
        return

    while True:
        try:
            message = _read()
            if message is None:
                return
            op = message.get("op")
            if op == "STATUS":
                _only(message, {"op"})
                result = _status(state)
            elif op == "OBSERVE":
                result = _observe(state, message)
            elif op == "THINK":
                result = _think(state, message)
            elif op == "STOP":
                _only(message, {"op"})
                _send({"op": "STOPPED", "instance_id": state["instance_id"]})
                return
            else:
                raise ValueError("unsupported operation")
            _send({"op": "RESULT", **result})
        except RuntimeError as exc:
            _send({"op": "ERROR", "error_code": "STEP_LIMIT", "message": str(exc)[:160]})
        except (ValueError, TypeError, KeyError) as exc:
            _send({"op": "ERROR", "error_code": "DENIED", "message": str(exc)[:160]})


if __name__ == "__main__":
    main()
