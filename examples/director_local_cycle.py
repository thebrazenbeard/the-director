"""A harmless one-shot local Director coordination demonstration.

The "worker" below is a fixture standing in for a software agent. No model,
network interface, external process, or paid provider is invoked. The example
creates and closes private temporary SQLite files, then removes them.

From repository root: python3 -m examples.director_local_cycle
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from runtime.director.coordinator import DirectorCoordinator
from runtime.director.messenger import DirectorMailbox


def main() -> None:
    goal = {
        "goal_id": "local_quality_demo",
        "objective": "Understand an illustrative quality metric",
        "metric": "quality",
        "target_value": 8,
        "direction": "INCREASE",
    }
    capabilities = [{
        "capability_id": "local_analysis",
        "agent_id": "demo_worker",
        "metric": "quality",
        "expected_delta": 2,
        "cost_units": 0,
    }]

    with TemporaryDirectory(prefix="director-example-") as temporary:
        directory = Path(temporary)
        with DirectorMailbox(directory / "mailbox.sqlite3") as mailbox:
            with DirectorCoordinator(directory / "coordinator.sqlite3", mailbox) as coordinator:
                coordinator.configure(goal, capabilities, max_missions=2)
                first = coordinator.tick()
                inbox = mailbox.poll("demo_worker")
                assert len(inbox) == 1
                assignment = inbox[0]

                # This is only a simulated software worker replying; neither
                # sender identity nor the content has been authenticated.
                mailbox.ack("demo_worker", assignment["message_id"])
                mailbox.reply(
                    sender_id="demo_worker",
                    in_reply_to=assignment["message_id"],
                    kind="MISSION_REPORT",
                    payload={"note": "Analytic simulation finished; no observation made"},
                    message_id="demo.report.1",
                )
                second = coordinator.tick()

                # Real verification requires evidence observed via a separate
                # path and deliberately confirmed by a trusted local operator.
                coordinator.observe({
                    "event_id": "demo.observation.1",
                    "claim_id": "quality",
                    "value": 8,
                    "evidence_class": "OBSERVATION",
                    "source_ref": "declared_operator_measurement",
                    "lineage_id": "demo_only",
                })
                coordinator.confirm_observation("demo.observation.1", confirms_success=True)
                third = coordinator.tick()
                print(json.dumps({
                    "first": first,
                    "assigned_to": assignment["recipient_id"],
                    "report_review": second["inbound_reports"],
                    "after_operator_confirmation": third["status"],
                    "external_effects": "NONE",
                }, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
