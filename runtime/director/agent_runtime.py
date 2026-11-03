"""Consent-started, separate-process **data-only** software-agent instance.

``IsolatedAgentRuntime`` receives a record returned by ``restore_agent_state``
and starts a fixed Python worker with a private temporary working directory,
isolated Python module loading (``-I -S``), bounded newline-JSON IPC, and only
STATUS/OBSERVE/THINK/STOP operations. Memory summaries are inert data, never
code. Nothing auto-starts, opens a port, runs a plugin, or touches external
devices. Process and protocol limits are not a general OS-level sandbox;
malicious executable code is categorically not an accepted input.

An explicit ``require_os_principal_isolation`` request fails closed before a
child process or working directory is created: there is currently no verified
OS identity/ACL isolation provider. The default data-only subprocess remains
backward-compatible, and ``status`` labels its actual security boundary.

Consent and source identity here are declared by the local caller, **not
cryptographically authenticated**. A source checksum and a local consent flag
are insufficient to establish the subjective identity of any conscious being.
The restored record is not crash-atomically installed into a live process; a
caller must separately address crash recovery before treating this as a
production handoff. The implementation is a deliberately opt-in reference.
"""

from __future__ import annotations

import json
import math
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_RECORD_KEYS = frozenset({
    "new_instance_id", "agent_id", "restored_from_instance_id", "capsule_id",
    "target_domain", "subject_kind", "state", "memory_manifest", "authority",
    "effects", "continuity_claim",
})
_APPROVAL_KEYS = frozenset({
    "approved", "agent_id", "new_instance_id", "target_domain", "purpose",
})
_MANIFEST_KEYS = frozenset({"input_count", "transferred_count", "omitted_count"})
_MAX_WIRE = 32768
_MAX_STEPS = 256
_ACTIVE_IDS: set[str] = set()
_ACTIVE_LOCK = threading.Lock()


def _identifier(value: object, name: str) -> str:
    if type(value) is not str or not _ID.fullmatch(value):
        raise ValueError(f"{name} must be a bounded identifier")
    return value


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
                          allow_nan=False).encode("ascii")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("data must be canonical JSON-compatible values") from exc


def _validate_record(record: Mapping[str, object], approval: Mapping[str, object]) -> dict[str, object]:
    """Reject effect grants, arbitrary program fields, and target mismatches.

    This validates a *record's declared structure*, not its author's identity.
    Provenance checks against a capsule and consent ledger occur in the
    existing ``restore_agent_state`` API before calling this constructor.
    """
    if not isinstance(record, Mapping) or set(record) != _RECORD_KEYS:
        raise ValueError("restored record has unexpected fields")
    if (record["subject_kind"] != "SOFTWARE_AGENT" or record["authority"] != "NOT_GRANTED"
            or record["effects"] != "NOT_EXECUTED"
            or record["continuity_claim"] != "INFORMATION_STATE_COPY_ONLY"):
        raise ValueError("record is not an inert, no-effect software-agent state")
    for key in ("new_instance_id", "agent_id", "restored_from_instance_id", "capsule_id", "target_domain"):
        _identifier(record[key], key)
    if record["new_instance_id"] == record["restored_from_instance_id"]:
        raise ValueError("cannot overwrite the source instance")
    if not isinstance(approval, Mapping) or set(approval) != _APPROVAL_KEYS:
        raise ValueError("separate runtime approval must contain exactly the specified fields")
    if approval["approved"] is not True or approval["purpose"] != "DATA_ONLY_LOCAL_RUNTIME":
        raise ValueError("explicit data-only process approval is required")
    for key in ("agent_id", "new_instance_id", "target_domain"):
        if _identifier(approval[key], "approval." + key) != record[key]:
            raise ValueError("runtime approval is not bound to this restored agent, instance and domain")

    # Reuse the strict capsule validator for the same in-repository data
    # schema. No serialized executable objects are ever rehydrated.
    from .agent_state import _state as validate_state

    state = validate_state(record["state"], include_memories=True)
    if _canonical(state) != _canonical(record["state"]):
        raise ValueError("restored state is not in the accepted canonical schema")
    manifest = record["memory_manifest"]
    if (not isinstance(manifest, Mapping) or set(manifest) != _MANIFEST_KEYS
            or any(type(manifest[key]) is not int or not 0 <= manifest[key] <= 32 for key in manifest)
            or manifest["transferred_count"] != len(state["memories"])
            or manifest["input_count"] != manifest["transferred_count"] + manifest["omitted_count"]):
        raise ValueError("invalid memory manifest")
    normalized = json.loads(_canonical(dict(record)))
    if len(_canonical(normalized)) > _MAX_WIRE - 1024:
        raise ValueError("restored record too large for the fixed IPC contract")
    return normalized


def _read_thread(process: subprocess.Popen[bytes], responses: queue.Queue[bytes | None]) -> None:
    assert process.stdout is not None
    try:
        while True:
            line = process.stdout.readline(_MAX_WIRE + 1)
            if not line:
                responses.put(None)
                return
            responses.put(line)
    except (OSError, ValueError):
        responses.put(None)


class IsolatedAgentRuntime:
    """A manually started, bounded local **process** holding copied state.

    Not a general Python sandbox. All executable behavior is hard-coded in the
    child script. The public ``process`` handle is provided for diagnostics;
    a trusted local caller controls it. Use as a context manager or ``close``.
    """

    def __init__(self, process: subprocess.Popen[bytes], working: tempfile.TemporaryDirectory[str],
                 instance_id: str, responses: queue.Queue[bytes | None], reader: threading.Thread,
                 request_timeout: float) -> None:
        self.process = process
        self._working = working
        self._instance_id = instance_id
        self._responses = responses
        self._reader = reader
        self._timeout = request_timeout
        self._closed = False
        self._request_lock = threading.RLock()

    @classmethod
    def start(
        cls, restored_record: Mapping[str, object], *,
        operator_approval: Mapping[str, object], max_steps: int = 32,
        request_timeout: float = 8.0,
        require_os_principal_isolation: bool = False,
    ) -> "IsolatedAgentRuntime":
        """Start one new local child after an explicit target-bound decision.

        ``restored_record`` should be the result of ``restore_agent_state``;
        this API cannot independently authenticate a claimed operator or
        detect whether another *host process* has already imported the record.

        The optional strict flag never silently falls back to a subprocess
        under the caller's identity. A future OS provider needs a separate
        reviewed implementation and real host qualification.
        """
        if type(require_os_principal_isolation) is not bool:
            raise ValueError("require_os_principal_isolation must be a boolean")
        if require_os_principal_isolation:
            raise RuntimeError("OS-principal isolation is unavailable: no verified provider configured")
        normalized = _validate_record(restored_record, operator_approval)
        if type(max_steps) is not int or not 1 <= max_steps <= _MAX_STEPS:
            raise ValueError("max_steps must be between 1 and 256")
        if type(request_timeout) not in (float, int) or not math.isfinite(request_timeout) or not 0.1 <= request_timeout <= 30:
            raise ValueError("request_timeout must be a bounded positive duration")
        instance_id = normalized["new_instance_id"]
        with _ACTIVE_LOCK:
            if instance_id in _ACTIVE_IDS:
                raise ValueError("this software instance is already active in this coordinator process")
            _ACTIVE_IDS.add(instance_id)

        working: tempfile.TemporaryDirectory[str] | None = None
        process: subprocess.Popen[bytes] | None = None
        reader: threading.Thread | None = None
        try:
            working = tempfile.TemporaryDirectory(prefix="director-agent-domain-")
            script = Path(__file__).with_name("agent_runtime_worker.py").resolve(strict=True)
            environment = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "TEMP", "TMP")
                           if key in os.environ}
            environment.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
            process = subprocess.Popen(
                [sys.executable, "-I", "-S", "-u", os.fspath(script)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                cwd=working.name, env=environment, close_fds=True, shell=False,
                bufsize=0,
            )
            responses: queue.Queue[bytes | None] = queue.Queue()
            reader = threading.Thread(target=_read_thread, args=(process, responses),
                                      name="director-agent-json-reader", daemon=True)
            reader.start()
            runtime = cls(process, working, instance_id, responses, reader, float(request_timeout))
            startup = runtime._exchange({"op": "INIT", "record": normalized, "max_steps": max_steps},
                                        startup=True)
            if startup.get("op") != "READY" or startup.get("instance_id") != instance_id:
                raise RuntimeError("isolated worker rejected the software-state record")
            return runtime
        except BaseException:
            if process is not None:
                try:
                    process.terminate()
                except OSError:
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                if process.stdin is not None:
                    process.stdin.close()
                if process.stdout is not None:
                    process.stdout.close()
            # Thread.start() itself can fail; joining a never-started Thread
            # would mask that error and skip the directory/identity cleanup.
            if reader is not None and reader.is_alive():
                reader.join(timeout=2)
            if working is not None:
                working.cleanup()
            with _ACTIVE_LOCK:
                _ACTIVE_IDS.discard(instance_id)
            raise

    def _exchange(self, request: Mapping[str, object], *, startup: bool = False) -> dict[str, object]:
        with self._request_lock:
            if self._closed or self.process.poll() is not None:
                raise RuntimeError("isolated software agent is no longer running")
            line = _canonical(dict(request))
            if len(line) >= _MAX_WIRE:
                raise ValueError("agent request exceeds the protocol limit")
            try:
                assert self.process.stdin is not None
                self.process.stdin.write(line + b"\n")
                self.process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise RuntimeError("agent subprocess input is unavailable") from exc
            try:
                response = self._responses.get(timeout=self._timeout)
            except queue.Empty as exc:
                self.close()
                raise RuntimeError("agent subprocess did not respond before timeout") from exc
            if response is None or len(response) > _MAX_WIRE or not response.endswith(b"\n"):
                raise RuntimeError("agent subprocess closed or sent invalid output")
            try:
                data = json.loads(response.decode("ascii"))
            except (UnicodeError, ValueError, TypeError) as exc:
                raise RuntimeError("invalid agent subprocess response") from exc
            if type(data) is not dict:
                raise RuntimeError("invalid agent subprocess result object")
            if data.get("op") == "ERROR":
                error = str(data.get("message", "worker rejected request"))
                if startup or data.get("error_code") == "STEP_LIMIT":
                    raise RuntimeError(error)
                raise ValueError(error)
            if data.get("op") not in (("READY",) if startup else ("RESULT", "STOPPED")):
                raise RuntimeError("unexpected agent subprocess response type")
            return data

    def _request(self, request: Mapping[str, object]) -> dict[str, object]:
        if not isinstance(request, Mapping) or request.get("op") not in ("STATUS", "OBSERVE", "THINK"):
            raise ValueError("only fixed STATUS/OBSERVE/THINK operations are allowed")
        data = self._exchange(request)
        data.pop("op", None)
        return data

    def status(self) -> dict[str, object]:
        """Return health and the honest OS-security boundary, not memories."""
        status = self._request({"op": "STATUS"})
        status["os_principal_isolation_status"] = "NOT_VERIFIED"
        status["isolation_mechanism"] = "SAME_ACCOUNT_SUBPROCESS"
        return status

    def observe(self, *, observation_id: str, metric: str, value: int | float) -> dict[str, object]:
        """Admit a *declared*, never independently verified metric reading."""
        _identifier(observation_id, "observation_id")
        _identifier(metric, "metric")
        if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 10**12:
            raise ValueError("value must be a finite bounded number")
        return self._request({"op": "OBSERVE", "observation_id": observation_id,
                              "metric": metric, "value": value})

    def think(self) -> dict[str, object]:
        """Run one fixed, bounded data-only analytic transition."""
        return self._request({"op": "THINK"})

    def close(self) -> None:
        """Attempt cooperative shutdown, then terminate only our child."""
        with self._request_lock:
            if self._closed:
                return
            self._closed = True
            try:
                if self.process.poll() is None and self.process.stdin is not None:
                    try:
                        self.process.stdin.write(b'{"op":"STOP"}\n')
                        self.process.stdin.flush()
                    except (BrokenPipeError, OSError, ValueError):
                        pass
                try:
                    self.process.wait(timeout=min(self._timeout, 2.0))
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=2)
            finally:
                if self.process.stdin is not None:
                    self.process.stdin.close()
                if self.process.stdout is not None:
                    self.process.stdout.close()
                self._reader.join(timeout=2)
                self._working.cleanup()
                with _ACTIVE_LOCK:
                    _ACTIVE_IDS.discard(self._instance_id)

    def __enter__(self) -> "IsolatedAgentRuntime":
        if self._closed:
            raise RuntimeError("agent process is already closed")
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


__all__ = ["IsolatedAgentRuntime"]
