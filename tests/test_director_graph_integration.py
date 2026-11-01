"""End-to-end contract: dependency planning belongs to the coordinator loop."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from runtime.director.coordinator import DirectorCoordinator
from runtime.director.messenger import DirectorMailbox


GOAL = {"goal_id": "graph.goal", "objective": "Understand an example quality metric",
        "metric": "quality", "target_value": 10, "direction": "INCREASE"}
CAPS = [
    {"capability_id": "alpha", "agent_id": "worker.alpha", "metric": "quality",
     "expected_delta": 2, "cost_units": 0},
    {"capability_id": "beta", "agent_id": "worker.beta", "metric": "quality",
     "expected_delta": 1, "cost_units": 0},
]


def node(name, depends=(), resource="channel"):
    return {"mission_id": name, "kind": "ANALYZE", "depends_on": list(depends),
            "resources": {resource: 1}, "priority": 40, "cost_units": 0}


def observed(event_id, claim_id, value):
    return {"event_id": event_id, "claim_id": claim_id, "value": value,
            "evidence_class": "OBSERVATION", "source_ref": "operator.claim",
            "lineage_id": "local.review"}


class GraphCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        parent = Path(self.temp.name)
        self.mailbox = DirectorMailbox(parent / "inbox.sqlite3")
        self.addCleanup(self.mailbox.close)
        self.coordinator_path = parent / "coordinator.sqlite3"
        self.coordinator = DirectorCoordinator(self.coordinator_path, self.mailbox)
        self.addCleanup(self.coordinator.close)
        self.coordinator.configure(GOAL, CAPS, max_missions=8)

    def configure_chain(self):
        return self.coordinator.configure_graph([node("alpha"), node("beta", ["alpha"])],
                                                {"channel": 1})

    def test_dependency_only_unlocks_after_operator_reviewed_observation(self):
        self.configure_chain()
        first = self.coordinator.tick()
        self.assertEqual(len(first["new_missions"]), 1)
        alpha = self.mailbox.poll("worker.alpha")[0]
        self.assertEqual(self.mailbox.poll("worker.beta"), [])

        self.mailbox.reply(sender_id="worker.alpha", in_reply_to=alpha["message_id"],
                           kind="MISSION_REPORT", payload={"done": True}, message_id="claimed.alpha")
        after_claim = self.coordinator.tick()
        self.assertFalse(after_claim["new_missions"])
        self.assertEqual(self.mailbox.poll("worker.beta"), [])
        self.assertEqual(self.coordinator.inspect()["graph"]["completed"], [])

        self.coordinator.observe(observed("verified.alpha", "completed.alpha", True))
        self.coordinator.confirm_observation("verified.alpha")
        approved = self.coordinator.confirm_graph_node_completed(
            "alpha", alpha["message_id"], "verified.alpha")
        self.assertEqual(approved["completion"], "OPERATOR_REVIEWED_LOCAL")
        self.assertFalse(approved["source_authenticated"])
        second = self.coordinator.tick()
        self.assertEqual(len(second["new_missions"]), 1)
        self.assertEqual(self.mailbox.poll("worker.beta")[0]["payload"]["capability_id"], "beta")

    def test_forged_report_and_unconfirmed_claim_never_unlock_dependency(self):
        self.configure_chain()
        self.coordinator.tick()
        alpha = self.mailbox.poll("worker.alpha")[0]
        self.mailbox.reply(sender_id="worker.alpha", in_reply_to=alpha["message_id"],
                           kind="MISSION_REPORT", payload={"verified": True}, message_id="forged.claim")
        self.coordinator.tick()
        self.coordinator.observe(observed("not.reviewed", "completed.alpha", True))
        with self.assertRaisesRegex(ValueError, "reviewed"):
            self.coordinator.confirm_graph_node_completed("alpha", alpha["message_id"], "not.reviewed")
        self.assertEqual(self.mailbox.poll("worker.beta"), [])

    def test_old_preassignment_completion_claim_cannot_unlock_dependency(self):
        self.configure_chain()
        self.coordinator.observe(observed("old.claim", "completed.alpha", True))
        self.coordinator.confirm_observation("old.claim")
        self.coordinator.tick()
        alpha = self.mailbox.poll("worker.alpha")[0]
        self.mailbox.reply(sender_id="worker.alpha", in_reply_to=alpha["message_id"],
                           kind="MISSION_REPORT", payload={"done": True}, message_id="report.after.old")
        self.coordinator.tick()
        with self.assertRaisesRegex(ValueError, "newer"):
            self.coordinator.confirm_graph_node_completed("alpha", alpha["message_id"], "old.claim")
        self.assertEqual(self.mailbox.poll("worker.beta"), [])

    def test_conflicting_completion_evidence_revokes_downstream_mission(self):
        self.configure_chain()
        self.coordinator.tick()
        alpha = self.mailbox.poll("worker.alpha")[0]
        self.mailbox.reply(sender_id="worker.alpha", in_reply_to=alpha["message_id"],
                           kind="MISSION_REPORT", payload={"done": True}, message_id="alpha.reply")
        self.coordinator.tick()
        self.coordinator.observe(observed("complete.one", "completed.alpha", True))
        self.coordinator.confirm_observation("complete.one")
        self.coordinator.confirm_graph_node_completed("alpha", alpha["message_id"], "complete.one")
        self.coordinator.tick()
        beta = self.mailbox.poll("worker.beta")[0]
        self.coordinator.observe(observed("complete.conflict", "completed.alpha", False))
        result = self.coordinator.tick()
        self.assertIn("alpha", self.coordinator.inspect()["graph"]["invalidated"])
        self.assertTrue(result["cancellation_message_ids"])
        self.assertEqual(self.mailbox.poll("worker.beta")[-1]["kind"], "MISSION_CANCEL")
        self.assertEqual(self.coordinator.inspect()["assignments"][-1]["control_state"], "CANCEL_REQUESTED")

    def test_resource_outage_cascades_cancellation_without_claiming_worker_stopped(self):
        self.configure_chain()
        self.coordinator.tick()
        alpha = self.mailbox.poll("worker.alpha")[0]
        self.coordinator.set_unavailable_graph_resources(["channel"])
        stopped = self.coordinator.tick()
        self.assertTrue(stopped["cancellation_message_ids"])
        self.assertFalse(stopped["worker_stopped_verified"])
        self.assertEqual(self.mailbox.poll("worker.alpha")[-1]["kind"], "MISSION_CANCEL")
        self.assertEqual(self.mailbox.poll("worker.beta"), [])
        self.assertEqual(self.coordinator.inspect()["graph"]["unavailable_resources"], ["channel"])

    def test_deadline_tick_expiry_cooperatively_cancels_active_mission(self):
        expiring = node("alpha")
        expiring["deadline_tick"] = 0
        self.coordinator.configure_graph([expiring, node("beta", ["alpha"])], {"channel": 1})
        self.coordinator.tick()
        self.assertEqual(self.mailbox.poll("worker.alpha")[0]["kind"], "MISSION_ASSIGNMENT")
        self.coordinator.set_graph_logical_tick(1)
        expired = self.coordinator.tick()
        self.assertEqual(len(expired["cancellation_message_ids"]), 1)
        self.assertEqual(self.mailbox.poll("worker.alpha")[-1]["kind"], "MISSION_CANCEL")
        self.assertEqual(self.coordinator.inspect()["graph"]["logical_tick"], 1)
        with self.assertRaisesRegex(ValueError, "backwards"):
            self.coordinator.set_graph_logical_tick(0)

    def test_graph_configuration_persists_and_cannot_change_after_dispatch(self):
        data = self.configure_chain()
        self.assertEqual(data["resource_capacities"], {"channel": 1})
        with self.assertRaisesRegex(ValueError, "registered"):
            self.coordinator.configure_graph([node("unknown")], {"channel": 1})
        self.coordinator.tick()
        self.coordinator.close()
        with DirectorCoordinator(self.coordinator_path, self.mailbox) as restored:
            self.assertEqual(restored.inspect()["graph"]["ready"], [])
            self.assertEqual(restored.configure_graph([node("alpha"), node("beta", ["alpha"])],
                                                      {"channel": 1}), data)
            with self.assertRaisesRegex(ValueError, "immutable"):
                restored.configure_graph([node("alpha")], {"channel": 1})

    def test_graph_ready_low_ranked_capability_is_not_starved_by_candidate_limit(self):
        # The ranked strategist prefers alpha (higher expected_delta), but
        # alpha depends on beta. Quota=1 must still dispatch graph-ready beta.
        limited_path = self.coordinator_path.with_name("limited.sqlite3")
        with DirectorCoordinator(limited_path, self.mailbox) as limited:
            limited.configure(GOAL, CAPS, max_missions=1)
            limited.configure_graph([node("alpha", ["beta"]), node("beta")],
                                    {"channel": 1})
            result = limited.tick()
            self.assertEqual(len(result["new_missions"]), 1)
            beta = self.mailbox.poll("worker.beta")
            self.assertEqual(len(beta), 1)
            self.assertEqual(beta[0]["payload"]["capability_id"], "beta")
            self.assertEqual(self.mailbox.poll("worker.alpha"), [])


if __name__ == "__main__":
    unittest.main()
