"""Opt-in, data-only transfer of *software agent* state between local instances.

This is not human consciousness transfer, a human-brain interface, a sentience
test, or proof of continuity of subjective experience. Restoring a capsule
constructs a new state record intended for a distinct software instance; it
does **not** create or run that instance, replace a person, install code, run
a worker, or grant effect authority. No dedicated credential
field is admitted, but **free-text memory can itself contain credentials**.
Callers must exclude or redact sensitive text before consenting to export.

The capsule checksum detects accidental corruption, **not malicious authors**:
anyone with the plaintext bytes can alter both content and digest. Consent and
approval records are claims by the local caller, not authenticated identities.
The optional state may contain private text; operator-selected storage must be
protected and no secrets should be placed in it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "director.software-agent-state.v1"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_MAX_BYTES = 32768
_MAX_MEMORIES = 32
_MAX_SUMMARY = 512
_GOAL_KEYS = frozenset({"goal_id", "objective", "metric", "target_value", "direction"})
_CONSENT_KEYS = frozenset(
    {"approved", "agent_id", "source_instance_id", "target_domain", "allow_memories", "operator_ref"}
)
_APPROVAL_KEYS = frozenset({"approved", "agent_id", "new_instance_id", "target_domain"})
_BODY_KEYS = frozenset(
    {"schema_version", "capsule_id", "subject_kind", "agent_id", "source_instance_id",
     "target_domain", "consent", "state", "memory_manifest"}
)
_MEMORY_MANIFEST_KEYS = frozenset({"input_count", "transferred_count", "omitted_count"})


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{label} must be a bounded identifier")
    return value


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("ascii")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("capsule must contain bounded JSON data only") from exc


def _keys(raw: object, expected: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise ValueError(f"{label} must have exactly the documented fields")
    return raw


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_SUMMARY:
        raise ValueError(f"{label} must be nonempty and at most {_MAX_SUMMARY} characters")
    return value


def _goal(raw: object) -> dict[str, object]:
    g = _keys(raw, _GOAL_KEYS, "state.goal")
    direction = g["direction"]
    if direction not in ("INCREASE", "DECREASE"):
        raise ValueError("goal direction must be INCREASE or DECREASE")
    value = g["target_value"]
    if type(value) is int:
        in_range = abs(value) <= 10**12
    elif type(value) is float:
        in_range = math.isfinite(value) and abs(value) <= 10**12
    else:
        in_range = False
    if not in_range:
        raise ValueError("goal target_value is too large")
    return {
        "goal_id": _identifier(g["goal_id"], "goal_id"),
        "objective": _text(g["objective"], "objective"),
        "metric": _identifier(g["metric"], "goal metric"),
        "target_value": value,
        "direction": direction,
    }


def _memories(raw: object) -> list[dict[str, str]]:
    if not isinstance(raw, list) or len(raw) > _MAX_MEMORIES:
        raise ValueError("state.memories must be a bounded list")
    seen: set[str] = set()
    result = []
    for entry in raw:
        m = _keys(entry, frozenset({"memory_id", "summary"}), "memory")
        memory_id = _identifier(m["memory_id"], "memory_id")
        if memory_id in seen:
            raise ValueError("duplicate memory_id")
        seen.add(memory_id)
        result.append({"memory_id": memory_id, "summary": _text(m["summary"], "summary")})
    return result


def _state(raw: object, *, include_memories: bool) -> dict[str, object]:
    s = _keys(raw, frozenset({"goal", "memories"}), "state")
    # No dedicated configurations, executable instructions, authority grants,
    # workers, host permissions, model credentials or arbitrary object graph.
    # Memory summary text can still *contain* secrets; caller must redact.
    memories = _memories(s["memories"]) if include_memories else []
    if not include_memories:
        # Still validate the caller's type and count without serializing data.
        if not isinstance(s["memories"], list) or len(s["memories"]) > _MAX_MEMORIES:
            raise ValueError("state.memories must be a bounded list")
    return {"goal": _goal(s["goal"]), "memories": memories}


def _memory_manifest(raw: object, *, included: int, allow_memories: bool) -> dict[str, int]:
    """Validate aggregate loss disclosure without retaining missing memory IDs.

    The omitted count is supplied by the exporter, not a verified statement
    about all memories the source agent may actually possess.
    """
    m = _keys(raw, _MEMORY_MANIFEST_KEYS, "memory manifest")
    if any(type(m[field]) is not int or not 0 <= m[field] <= _MAX_MEMORIES for field in _MEMORY_MANIFEST_KEYS):
        raise ValueError("memory manifest counters must be bounded nonnegative integers")
    if (m["transferred_count"] != included
            or m["input_count"] != m["transferred_count"] + m["omitted_count"]
            or (allow_memories and m["omitted_count"] != 0)
            or (not allow_memories and m["transferred_count"] != 0)):
        raise ValueError("memory manifest disagrees with transfer scope or contents")
    return dict(m)


def _consent(raw: object, agent_id: str, source_id: str, target: str) -> dict[str, object]:
    c = _keys(raw, _CONSENT_KEYS, "consent")
    if c["approved"] is not True or type(c["allow_memories"]) is not bool:
        raise ValueError("explicit consent and a memory scope are required")
    if (c["agent_id"], c["source_instance_id"], c["target_domain"]) != (
        agent_id, source_id, target
    ):
        raise ValueError("consent does not bind this agent, source, and target")
    return {
        "approved": True,
        "agent_id": _identifier(c["agent_id"], "consent.agent_id"),
        "source_instance_id": _identifier(c["source_instance_id"], "consent.source_instance_id"),
        "target_domain": _identifier(c["target_domain"], "consent.target_domain"),
        "allow_memories": c["allow_memories"],
        "operator_ref": _identifier(c["operator_ref"], "operator_ref"),
    }


def export_agent_state(
    *, capsule_id: str, agent_id: str, source_instance_id: str,
    target_domain: str, state: Mapping[str, object], consent: Mapping[str, object]
) -> bytes:
    """Return a canonical checksum-bound, plaintext software-state capsule.

    Consent is *declared* by the caller and bound to source and target. Memory
    is excluded unless explicitly allowed. Credentials and executable config
    are never accepted as dedicated state fields. Opted-in memory text is not
    automatically redacted or classified; do not export secrets as memories.
    """
    capsule_id = _identifier(capsule_id, "capsule_id")
    agent_id = _identifier(agent_id, "agent_id")
    source_instance_id = _identifier(source_instance_id, "source_instance_id")
    target_domain = _identifier(target_domain, "target_domain")
    c = _consent(consent, agent_id, source_instance_id, target_domain)
    body = {
        "schema_version": SCHEMA_VERSION,
        "capsule_id": capsule_id,
        "subject_kind": "SOFTWARE_AGENT",
        "agent_id": agent_id,
        "source_instance_id": source_instance_id,
        "target_domain": target_domain,
        "consent": c,
        "state": _state(state, include_memories=c["allow_memories"]),
    }
    input_memories = state["memories"]
    body["memory_manifest"] = {
        "input_count": len(input_memories),
        "transferred_count": len(body["state"]["memories"]),
        "omitted_count": len(input_memories) - len(body["state"]["memories"]),
    }
    raw = _canonical(body)
    envelope = _canonical({"body": body, "sha256": hashlib.sha256(raw).hexdigest()})
    if len(envelope) > _MAX_BYTES:
        raise ValueError("capsule exceeds 32768 bytes")
    return envelope


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON key")
        out[key] = value
    return out


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-JSON numeric constant: {value}")


def _parse(capsule: bytes) -> dict[str, Any]:
    if not isinstance(capsule, bytes) or not capsule or len(capsule) > _MAX_BYTES:
        raise ValueError("capsule must be bounded encoded bytes")
    try:
        parsed = json.loads(capsule.decode("ascii"), object_pairs_hook=_reject_duplicates,
                            parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("invalid capsule JSON") from exc
    envelope = _keys(parsed, frozenset({"body", "sha256"}), "capsule envelope")
    if _canonical(envelope) != capsule:
        raise ValueError("capsule encoding must be canonical")
    digest = envelope["sha256"]
    if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
        raise ValueError("invalid digest")
    if not hmac.compare_digest(hashlib.sha256(_canonical(envelope["body"])).hexdigest(), digest):
        raise ValueError("capsule checksum mismatch")
    body = _keys(envelope["body"], _BODY_KEYS, "capsule body")
    if body["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported capsule schema")
    if body["subject_kind"] != "SOFTWARE_AGENT":
        raise ValueError("only software agent state can be restored")
    agent = _identifier(body["agent_id"], "agent_id")
    source = _identifier(body["source_instance_id"], "source_instance_id")
    target = _identifier(body["target_domain"], "target_domain")
    _identifier(body["capsule_id"], "capsule_id")
    consent = _consent(body["consent"], agent, source, target)
    state = _state(body["state"], include_memories=consent["allow_memories"])
    if _canonical(state) != _canonical(body["state"]):
        raise ValueError("state does not match declared memory scope")
    _memory_manifest(body["memory_manifest"], included=len(state["memories"]),
                     allow_memories=consent["allow_memories"])
    return {"body": body, "sha256": digest}


class AgentStateLedger:
    """Local SQLite replay/collision ledger; no agent content or secret stored.

    The ledger is necessary for durable duplicate-import detection. It is not
    an authentication service, confidential store, or distributed lock.
    Each caller selects its path; opening the class is the only I/O trigger.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        name = os.fspath(path)
        if not name or name == ":memory:":
            raise ValueError("ledger requires an explicit durable local file path")
        p = Path(name)
        if p.is_symlink():
            raise ValueError("ledger must not be a symbolic link")
        if not p.exists():
            # On POSIX, SQLite would otherwise create a file using the
            # caller's possibly broad umask. Existing file mode is unchanged.
            flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                fd = os.open(p, flags, 0o600)
            except FileExistsError:
                pass  # another local caller may have created it meanwhile
            else:
                os.close(fd)
        self._conn = sqlite3.connect(name, timeout=5.0, isolation_level=None)
        try:
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA journal_mode=DELETE")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS restored_capsules ("
                "capsule_id TEXT PRIMARY KEY, digest TEXT NOT NULL, "
                "source_instance_id TEXT NOT NULL, new_instance_id TEXT NOT NULL UNIQUE, "
                "agent_id TEXT NOT NULL, target_domain TEXT NOT NULL)"
            )
        except BaseException:
            self._conn.close()
            raise

    def _admit(self, body: Mapping[str, object], digest: str, new_instance_id: str) -> None:
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                "INSERT INTO restored_capsules "
                "(capsule_id, digest, source_instance_id, new_instance_id, agent_id, target_domain) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (body["capsule_id"], digest, body["source_instance_id"],
                 new_instance_id, body["agent_id"], body["target_domain"]),
            )
            self._conn.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            self._conn.execute("ROLLBACK")
            raise ValueError("capsule already restored or target instance already used") from exc
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def _check_available(self, capsule_id: str, new_instance_id: str) -> None:
        """Read-only collision check; admission must independently recheck."""
        row = self._conn.execute(
            "SELECT capsule_id, new_instance_id FROM restored_capsules "
            "WHERE capsule_id=? OR new_instance_id=? LIMIT 1",
            (capsule_id, new_instance_id),
        ).fetchone()
        if row is not None:
            raise ValueError("capsule already restored or target instance already used")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "AgentStateLedger":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


def _preflight(
    capsule: bytes, *, target_domain: str, new_instance_id: str,
    operator_approval: Mapping[str, object], ledger: AgentStateLedger,
) -> tuple[dict[str, Any], str, str]:
    parsed = _parse(capsule)
    body = parsed["body"]
    target_domain = _identifier(target_domain, "target_domain")
    new_instance_id = _identifier(new_instance_id, "new_instance_id")
    if target_domain != body["target_domain"]:
        raise ValueError("target domain differs from capsule's consented domain")
    if new_instance_id == body["source_instance_id"]:
        raise ValueError("cannot overwrite the source instance")
    a = _keys(operator_approval, _APPROVAL_KEYS, "operator_approval")
    if a["approved"] is not True or (
        a["agent_id"], a["new_instance_id"], a["target_domain"]
    ) != (body["agent_id"], new_instance_id, target_domain):
        raise ValueError("separate target-bound operator approval required")
    if not isinstance(ledger, AgentStateLedger):
        raise ValueError("durable AgentStateLedger required for anti-replay")
    ledger._check_available(body["capsule_id"], new_instance_id)
    return parsed, target_domain, new_instance_id


def preview_agent_state_restore(
    capsule: bytes, *, target_domain: str, new_instance_id: str,
    operator_approval: Mapping[str, object], ledger: AgentStateLedger,
) -> dict[str, object]:
    """Read-only capsule validation; does not consume the local replay slot.

    The caller has already instantiated its chosen ledger. Preview neither
    creates a database, saves a state, reserves an instance, authenticates the
    claimed approval, nor starts a process. A concurrent restore can invalidate
    this snapshot immediately, so actual import repeats every check.
    """
    parsed, target_domain, new_instance_id = _preflight(
        capsule, target_domain=target_domain, new_instance_id=new_instance_id,
        operator_approval=operator_approval, ledger=ledger,
    )
    body = parsed["body"]
    # Deliberately omit source memory content, including consented memory
    # summaries. The counts are exporter-declared and could be spoofed by
    # someone able to rewrite the plaintext checksum-bound capsule.
    return {
        "status": "ELIGIBLE_AT_PREVIEW",
        "capsule_id": body["capsule_id"],
        "agent_id": body["agent_id"],
        "restored_from_instance_id": body["source_instance_id"],
        "new_instance_id": new_instance_id,
        "target_domain": target_domain,
        "memory_manifest": dict(body["memory_manifest"]),
        "authority": "NOT_GRANTED",
        "executable": False,
        "source_authenticated": False,
        "continuity_claim": "INFORMATION_STATE_COPY_ONLY",
    }


def restore_agent_state(
    capsule: bytes, *, target_domain: str, new_instance_id: str,
    operator_approval: Mapping[str, object], ledger: AgentStateLedger,
) -> dict[str, object]:
    """Admit data for a **different** software instance after local approval.

    No process is created or executed; no effects or authorizations are
    restored. Explicit memory text can contain secrets; the caller must
    redact it. A local replay marker is persisted **before** this function
    returns the record, so this is NOT atomic activation or a crash-recoverable
    runtime migration protocol. It cannot prove a claimed approver's identity.
    """
    parsed, target_domain, new_instance_id = _preflight(
        capsule, target_domain=target_domain, new_instance_id=new_instance_id,
        operator_approval=operator_approval, ledger=ledger,
    )
    body = parsed["body"]
    ledger._admit(body, parsed["sha256"], new_instance_id)
    # New identity is unambiguous and detached. This record cannot instruct a
    # host or acquire additional permissions from source-state metadata.
    return {
        "new_instance_id": new_instance_id,
        "agent_id": body["agent_id"],
        "restored_from_instance_id": body["source_instance_id"],
        "capsule_id": body["capsule_id"],
        "target_domain": target_domain,
        "subject_kind": "SOFTWARE_AGENT",
        "state": json.loads(_canonical(body["state"])),
        "memory_manifest": dict(body["memory_manifest"]),
        "authority": "NOT_GRANTED",
        "effects": "NOT_EXECUTED",
        "continuity_claim": "INFORMATION_STATE_COPY_ONLY",
    }


__all__ = ["SCHEMA_VERSION", "AgentStateLedger", "export_agent_state",
           "preview_agent_state_restore", "restore_agent_state"]
