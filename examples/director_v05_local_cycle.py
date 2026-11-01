"""Zero-cost, ephemeral demonstration of The Director's v0.5 local contracts.

Runs an explicitly configured bidirectional HMAC analytical message exchange,
one hypothesis-probe recommendation, and a paused/quarantined recovery copy.
It neither executes a mission nor starts a service, socket or model provider.
Keys are generated only for this disposable test, never for persistent use.

From the repository root::

    python3 -B -m examples.director_v05_local_cycle
"""

from __future__ import annotations

import json
import secrets
import tempfile
import time
from pathlib import Path

from runtime.director.auth_channel import AuthenticatedChannel
from runtime.director.coordinator import DirectorCoordinator
from runtime.director.messenger import DirectorMailbox
from runtime.director.reasoning import LocalReasoner
from runtime.director.recovery_copy import stage_recovery_copy
from runtime.director.secure_worker import VerifiedWorkerInbox


def run() -> dict[str, object]:
    goal = {"goal_id": "demo.local", "objective": "Analyze declared quality evidence",
            "metric": "quality", "target_value": 8, "direction": "INCREASE"}
    capability = {"capability_id": "review", "agent_id": "worker_alpha",
                  "metric": "quality", "expected_delta": 1, "cost_units": 0}
    keys = {"worker_alpha": secrets.token_bytes(32),
            "director.worker_alpha": secrets.token_bytes(32)}
    with tempfile.TemporaryDirectory(prefix="director-v05-demo-") as directory:
        root = Path(directory)
        now = int(time.time())
        with DirectorMailbox(root / "messages.sqlite3") as mailbox:
            with AuthenticatedChannel(root / "director-auth.sqlite3", keys) as director_auth:
                with AuthenticatedChannel(root / "worker-auth.sqlite3", keys) as worker_auth:
                    coordinator_path = root / "coordinator.sqlite3"
                    with DirectorCoordinator(coordinator_path, mailbox,
                                             auth_channel=director_auth,
                                             sign_outbound=True) as director:
                        director.configure(goal, [capability], max_missions=2)
                        first = director.tick(auth_now_utc=now)
                        worker = VerifiedWorkerInbox(mailbox, worker_auth, "worker_alpha")
                        assignment = worker.pending()[0]
                        accepted = worker.verify_message(assignment, now_utc=now)
                        assert accepted["kind"] == "MISSION_ASSIGNMENT"
                        assert accepted["effect_status"] == "NOT_EXECUTED"

                        report = worker_auth.seal(
                            sender_id="worker_alpha", recipient_id="director",
                            kind="MISSION_REPORT",
                            payload={"in_reply_to": assignment["message_id"],
                                     "body": {"finding": "needs independent human review"}},
                            issued_at=now, expires_at=now + 300)
                        mailbox.send(sender_id="untrusted_transport", recipient_id="director",
                                     kind="SIGNED_ENVELOPE",
                                     payload={"envelope": report.decode("ascii")})
                        second = director.tick(auth_now_utc=now)
                        assert second["inbound_reports"][0]["review_status"] == \
                            "MATCHED_AUTHENTICATED_UNVERIFIED"

                        proposed = LocalReasoner().suggest_discriminating_observations(
                            director.inspect()["world"], [
                                {"hypothesis_id": "h1", "predictions": {"quality": 3}},
                                {"hypothesis_id": "h2", "predictions": {"quality": 6}},
                            ])
                        assert proposed["observations"][0]["metric"] == "quality"
                        director.set_paused(True)
                        destination = root / "recovery-quarantined.sqlite3"
                        recovery = stage_recovery_copy(
                            coordinator_path, destination,
                            operator_approval={
                                "approved": True, "purpose": "PAUSED_QUARANTINE_DATA_ONLY",
                                "source_goal_id": goal["goal_id"],
                                "destination_path": str(destination.resolve()),
                                "allow_historical_inflight": False,
                            })
                        with DirectorMailbox(root / "inert-mailbox.sqlite3") as inert_mailbox:
                            with DirectorCoordinator(destination, inert_mailbox,
                                                     auth_channel=director_auth,
                                                     sign_outbound=True) as quarantined:
                                assert quarantined.tick()["status"] == "QUARANTINED"
                                assert not inert_mailbox.poll("worker_alpha")
                        return {
                            "mode": first["authentication_mode"],
                            "signed_assignment": accepted["sender_authenticated"],
                            "signed_report": second["inbound_reports"][0]["review_status"],
                            "hypothesis_probe": proposed["observations"][0]["metric"],
                            "recovery_quarantined": recovery["quarantined"],
                            "external_effects": "NONE", "budget_usd": 0,
                        }


if __name__ == "__main__":
    print(json.dumps(run(), sort_keys=True, indent=2))
