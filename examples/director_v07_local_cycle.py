"""Source-only temporary v0.7 demonstration; no listener or worker process.

Run with ``python -B -m examples.director_v07_local_cycle``. The POSIX
adapter performs a real local read; deadlines are cooperative, not hard limits.
The Linux broker's peer UID is synthetic and does not test OS isolation.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from runtime.director.broker import Broker, BrokerError
from runtime.director.broker_socket import require_linux
from runtime.director.coordinator import DirectorCoordinator
from runtime.director.local_engine import LocalReasoningEngine, _read_json
from runtime.director.messenger import DirectorMailbox
from runtime.director.unattended_qualification import assess_host_inventory


def run():
    result = {"budget_usd": 0, "scope": "SOURCE_ONLY",
              "lappy_qualification": "ABSENT", "external_effects": "NONE"}
    with tempfile.TemporaryDirectory(prefix="director-v07-demo-") as directory:
        root = Path(directory)
        source = root / "source.json"
        source.write_text('{"value":"blue"}', encoding="utf-8")
        if os.name == "posix":
            with DirectorMailbox(root / "mail.db") as mailbox:
                with DirectorCoordinator(root / "director.db", mailbox) as director:
                    director.configure({"goal_id": "demo", "objective": "Assess quality",
                                        "metric": "quality", "target_value": 8,
                                        "direction": "INCREASE"}, [
                        {"capability_id": metric, "agent_id": "worker", "metric": metric,
                         "expected_delta": 0, "cost_units": 0}
                        for metric in ("quality", "signal")])
                    director.configure_graph([
                        {"mission_id": metric, "kind": "OBSERVE", "depends_on": [],
                         "resources": {}, "priority": 1, "cost_units": 0}
                        for metric in ("quality", "signal")], {})
                    engine = LocalReasoningEngine(director)
                    engine.register_source(source_id="demo.source", capability_id="signal",
                                           metric="signal", path=source, key="value")
                    report = engine.run(hypotheses=[
                        {"hypothesis_id": "blue", "predictions": {"signal": "blue"}},
                        {"hypothesis_id": "red", "predictions": {"signal": "red"}}],
                        max_steps=1, max_bytes=1024, max_seconds=2)
                    assert report["local_io_status"] == "LOCAL_JSON_READ_COMPLETED"
                    assert report["after"]["world_revision"] == 1
                    assert not report["success_confirmed"] and not mailbox.poll("worker")
                    result["local_reasoning"] = {
                        "io": report["local_io_status"], "classification": report["classification"],
                        "truth": report["truth_status"], "before_revision": report["before"]["world_revision"],
                        "after_revision": report["after"]["world_revision"],
                        "conditional_hypotheses": report["conditional_hypotheses"],
                        "stop_reason": report["stop_reason"], "deadline": report["deadline_kind"]}
        else:
            try:
                _read_json(str(source), 1024, time.monotonic() + 2)
            except ValueError:
                result["local_reasoning"] = "FAIL_CLOSED_UNSUPPORTED_SAFE_FILE_READ"
            else:
                raise AssertionError("unsupported file adapter did not fail closed")

        if sys.platform == "linux":
            broker = Broker(root / "broker.db")
            try:
                uid = os.getuid() + 10001  # Synthetic only; no peer connection occurs.
                broker.register("demo_worker", uid)
                broker.admit("demo_worker", "demo_command", "demo_nonce",
                             {"assertion": "data only"}, broker.epoch)
                poll = broker.handle(uid, {"version": 1, "op": "POLL", "request_id": "poll",
                                           "epoch": broker.epoch})
                command = poll["command"]
                ack = broker.handle(uid, {"version": 1, "op": "ACK_RECEIPT", "request_id": "ack",
                                          "epoch": broker.epoch, "command_id": command["id"],
                                          "delivery_token": command["delivery_token"]})
                assert ack["status"] == "RECEIVED" and not ack["effect_verified"]
                result["broker"] = {"identity": "SYNTHETIC_NOT_OS_VERIFIED",
                                    "receipt": ack["status"], "effect_verified": False}
            finally:
                broker.close()
        else:
            try:
                require_linux()
            except BrokerError:
                result["broker"] = "FAIL_CLOSED_UNSUPPORTED_NATIVE_ISOLATION"
            else:
                raise AssertionError("unsupported transport did not fail closed")
    host = assess_host_inventory()
    assert host["status"] == "UNQUALIFIED_HOST" and not host["qualified"]
    result["host_inventory"] = host["status"]
    result["host_release_decision"] = host["release_decision"]
    return result


if __name__ == "__main__":
    print(json.dumps(run(), sort_keys=True, indent=2))
