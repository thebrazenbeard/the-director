"""No-cost, temporary v0.6 qualification path with zero external effects.

Run from the repository root::

    python3 -B -m examples.director_v06_data_only_cycle

This demonstrates read-only hypothesis-to-DAG assessment, a signed command
recorded in a worker-owned receipt ledger, inert software-state restoration
after a simulated lost response, and explicit *rejection* of unqualified
OS isolation and unattended operation. Nothing launches an agent, ACKs a
mission, joins the network, installs a service, or calls a paid model.
"""

from __future__ import annotations

import json
import secrets
import tempfile
import time
from pathlib import Path

from runtime.director.agent_runtime import IsolatedAgentRuntime
from runtime.director.agent_state import (
    AgentStateLedger, export_agent_state, restore_agent_state, resume_agent_state_restore,
)
from runtime.director.auth_channel import AuthenticatedChannel
from runtime.director.coordinator import DirectorCoordinator
from runtime.director.messenger import DirectorMailbox
from runtime.director.secure_worker import VerifiedWorkerInbox
from runtime.director.unattended_qualification import assess_unattended_readiness
from runtime.director.worker_receipts import DurableWorkerReceipts


def run() -> dict[str, object]:
    goal = {"goal_id": "demo.v06", "objective": "Review declared quality",
            "metric": "quality", "target_value": 8, "direction": "INCREASE"}
    capabilities = [
        {"capability_id": "quality_probe", "agent_id": "worker.quality",
         "metric": "quality", "expected_delta": 1, "cost_units": 0},
        {"capability_id": "signal_probe", "agent_id": "worker.signal",
         "metric": "signal", "expected_delta": 0, "cost_units": 0},
    ]
    graph = [
        {"mission_id": name, "kind": "OBSERVE", "depends_on": [],
         "resources": {"reader": 1}, "priority": 10, "cost_units": 0}
        for name in ("quality_probe", "signal_probe")
    ]
    rivals = [
        {"hypothesis_id": "red", "predictions": {"signal": "red"}},
        {"hypothesis_id": "blue", "predictions": {"signal": "blue"}},
    ]
    keys = {route: secrets.token_bytes(32) for route in (
        "director.worker.quality", "director.worker.signal",
        "worker.quality", "worker.signal")}
    quality_worker_keys = {
        route: keys[route] for route in ("director.worker.quality", "worker.quality")
    }
    now = int(time.time())
    with tempfile.TemporaryDirectory(prefix="director-v06-demo-") as directory:
        root = Path(directory)
        with DirectorMailbox(root / "messages.sqlite3") as mailbox:
            with AuthenticatedChannel(root / "director-auth.sqlite3", keys) as director_auth:
                with AuthenticatedChannel(root / "worker-auth.sqlite3", quality_worker_keys) as worker_auth:
                    assert not worker_auth.has_sender_key("director.worker.signal")
                    assert not worker_auth.has_sender_key("worker.signal")
                    with DirectorCoordinator(root / "coordinator.sqlite3", mailbox,
                                             auth_channel=director_auth,
                                             sign_outbound=True) as director:
                        director.configure(goal, capabilities, max_missions=2)
                        director.configure_graph(graph, {"reader": 2})
                        assessment = director.assess_decision_cycle(hypotheses=rivals)
                        assert assessment["probe_proposals"][0]["mapping_status"] == \
                            "REGISTERED_DAG_READY"
                        assert not mailbox.poll("worker.signal")

                        director.tick(auth_now_utc=now)
                        worker = VerifiedWorkerInbox(mailbox, worker_auth, "worker.quality")
                        signed_command = worker.pending()[0]
                        with DurableWorkerReceipts(root / "worker-receipts.sqlite3", worker) as receipts:
                            receipt = receipts.accept(signed_command, now_utc=now)
                            assert receipt["state"] == "PENDING_REVIEW"
                            assert receipt["effect_status"] == "NOT_EXECUTED"
                        with DurableWorkerReceipts(root / "worker-receipts.sqlite3", worker) as receipts:
                            assert receipts.accept(signed_command, now_utc=now) == receipt
                        assert worker.pending(), "durable verification must not ACK the mailbox"

                        readiness = assess_unattended_readiness(director, now_utc=now)
                        assert readiness["status"] != "QUALIFIED"

                        capsule = export_agent_state(
                            capsule_id="demo.capsule", agent_id="demo.agent",
                            source_instance_id="demo.source", target_domain="demo.domain",
                            state={"goal": goal, "memories": []},
                            consent={"approved": True, "agent_id": "demo.agent",
                                     "source_instance_id": "demo.source", "target_domain": "demo.domain",
                                     "allow_memories": False, "operator_ref": "local.demo"})
                        approved = {"approved": True, "agent_id": "demo.agent",
                                    "new_instance_id": "demo.restored", "target_domain": "demo.domain"}
                        ledger_path = root / "state-ledger.sqlite3"
                        with AgentStateLedger(ledger_path) as ledger:
                            restore_agent_state(capsule, target_domain="demo.domain",
                                                new_instance_id="demo.restored",
                                                operator_approval=approved, ledger=ledger)
                        with AgentStateLedger(ledger_path) as ledger:
                            resumed = resume_agent_state_restore(
                                capsule, target_domain="demo.domain",
                                new_instance_id="demo.restored",
                                operator_approval=approved, ledger=ledger)
                        assert resumed["effects"] == "NOT_EXECUTED"
                        try:
                            IsolatedAgentRuntime.start(
                                resumed,
                                operator_approval={**approved, "purpose": "DATA_ONLY_LOCAL_RUNTIME"},
                                require_os_principal_isolation=True,
                            )
                        except RuntimeError as exc:
                            assert "OS-principal isolation is unavailable" in str(exc)
                        else:
                            raise AssertionError("unqualified OS isolation must fail closed")
                        return {
                            "budget_usd": 0,
                            "hypothesis_probe": assessment["probe_proposals"][0]["metric"],
                            "probe_dispatch": assessment["probe_proposals"][0]["dispatch_status"],
                            "worker_receipt": receipt["state"],
                            "agent_data_recovered": resumed["continuity_claim"],
                            "unattended_status": readiness["status"],
                            "os_isolation": "REJECTED_UNQUALIFIED",
                            "external_effects": "NONE",
                        }


if __name__ == "__main__":
    print(json.dumps(run(), sort_keys=True, indent=2))
