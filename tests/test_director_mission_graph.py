"""Dependency/resource scheduling evidence for the pure local mission graph."""

from __future__ import annotations

import copy
import unittest

from runtime.director.mission_graph import MissionGraph


def task(name, deps=(), resources=None, *, priority=0, deadline=None):
    row = {
        "mission_id": name,
        "kind": "ANALYZE",
        "depends_on": list(deps),
        "resources": dict(resources or {}),
        "priority": priority,
        "cost_units": 0,
    }
    if deadline is not None:
        row["deadline_tick"] = deadline
    return row


class MissionGraphTests(unittest.TestCase):
    def test_multistage_dependency_chain_issues_only_ready_analytical_work(self):
        # S1E6-like prerequisite chain: information before analysis before review.
        graph = MissionGraph([
            task("review", ["analysis"]),
            task("gather"),
            task("analysis", ["gather"]),
        ], {})
        self.assertEqual([m["mission_id"] for m in graph.ready()], ["gather"])
        self.assertEqual([m["mission_id"] for m in graph.ready(completed={"gather"})], ["analysis"])
        self.assertEqual([m["mission_id"] for m in graph.ready(completed={"gather", "analysis"})], ["review"])
        self.assertEqual(graph.ready(completed={"gather", "analysis", "review"}), [])

    def test_ready_missions_are_never_effect_permissions(self):
        graph = MissionGraph([task("analysis")], {})
        ready = graph.ready()[0]
        self.assertEqual(ready["effect_status"], "NOT_EXECUTED")
        self.assertEqual(ready["effect_authority"], "NOT_GRANTED")
        self.assertTrue(ready["requires_operator_authorization_for_effects"])

    def test_shared_resource_cannot_be_double_booked(self):
        graph = MissionGraph([
            task("low", resources={"channel": 1}, priority=1),
            task("high", resources={"channel": 1}, priority=9),
            task("independent"),
        ], {"channel": 1})
        self.assertEqual([m["mission_id"] for m in graph.ready()], ["high", "independent"])
        self.assertEqual(
            [m["mission_id"] for m in graph.ready(in_progress={"high"})],
            ["independent"],
        )

    def test_resource_outage_produces_cascaded_cancellation_recommendation(self):
        # S2E8: shared transfer channel offline disrupts multiple chains.
        graph = MissionGraph([
            task("send_a", resources={"channel": 1}),
            task("verify_a", ["send_a"]),
            task("report_a", ["verify_a"]),
            task("send_b", resources={"channel": 1}),
            task("safe_local_review"),
        ], {"channel": 1})
        result = graph.cascade_blocked(unavailable_resources={"channel"})
        self.assertEqual(result["directly_blocked"], ["send_a", "send_b"])
        self.assertEqual(result["downstream_blocked"], ["report_a", "verify_a"])
        self.assertEqual(result["recommended_cancellations"], ["report_a", "send_a", "send_b", "verify_a"])
        self.assertEqual([m["mission_id"] for m in graph.ready(unavailable_resources={"channel"})], ["safe_local_review"])
        self.assertEqual(result["effect_status"], "NOT_EXECUTED")

    def test_resource_outage_does_not_cancel_completed_prerequisite(self):
        graph = MissionGraph([
            task("sent", resources={"channel": 1}, deadline=2),
            task("review", ["sent"]),
        ], {"channel": 1})
        # The channel is now offline, but already-completed work remains done.
        result = graph.cascade_blocked(
            unavailable_resources={"channel"}, completed={"sent"}, now_tick=10,
        )
        self.assertFalse(result["recommended_cancellations"])
        self.assertEqual(
            [m["mission_id"] for m in graph.ready(completed={"sent"}, unavailable_resources={"channel"}, now_tick=10)],
            ["review"],
        )

    def test_failed_prerequisite_cascades_but_unrelated_branch_remains(self):
        graph = MissionGraph([task("root"), task("child", ["root"]), task("grandchild", ["child"]), task("other")], {})
        result = graph.cascade_blocked(failed_or_cancelled={"root"})
        self.assertEqual(result["directly_blocked"], ["root"])
        self.assertEqual(result["downstream_blocked"], ["child", "grandchild"])
        self.assertEqual([m["mission_id"] for m in graph.ready(failed_or_cancelled={"root"})], ["other"])

    def test_deadline_does_not_just_claim_succeeded(self):
        graph = MissionGraph([task("time_sensitive", deadline=4), task("dependent", ["time_sensitive"])], {})
        self.assertEqual([m["mission_id"] for m in graph.ready(now_tick=4)], ["time_sensitive"])
        blocked = graph.cascade_blocked(now_tick=5)
        self.assertEqual(blocked["directly_blocked"], ["time_sensitive"])
        self.assertEqual(blocked["downstream_blocked"], ["dependent"])
        self.assertEqual(graph.ready(now_tick=5), [])

    def test_deadlines_sorted_before_priority_tiebreak(self):
        graph = MissionGraph([
            task("late", priority=10, deadline=50),
            task("early", priority=0, deadline=10),
            task("none", priority=100),
        ], {})
        self.assertEqual([m["mission_id"] for m in graph.ready()], ["early", "late", "none"])

    def test_resource_capacity_override_blocks_without_mutating_defaults(self):
        graph = MissionGraph([task("one", resources={"network": 1}), task("two", resources={"network": 1})], {"network": 2})
        self.assertEqual(len(graph.ready()), 2)
        self.assertEqual(len(graph.ready(available_resources={"network": 1})), 1)
        self.assertEqual(len(graph.ready()), 2)
        with self.assertRaises(ValueError):
            graph.ready(available_resources={"undeclared": 3})

    def test_mission_inputs_defensively_copied(self):
        original = [task("read", resources={"cpu": 1})]
        capacities = {"cpu": 1}
        original_deepcopy = copy.deepcopy(original)
        graph = MissionGraph(original, capacities)
        original[0]["mission_id"] = "mutated"
        capacities["cpu"] = 0
        result = graph.ready()
        self.assertEqual(result[0]["mission_id"], original_deepcopy[0]["mission_id"])
        result[0]["resources"]["cpu"] = 100
        self.assertEqual(graph.ready()[0]["resources"]["cpu"], 1)

    def test_order_invariant(self):
        nodes = [task("a"), task("b", ["a"]), task("c"), task("d", ["a"])]
        graph = MissionGraph(nodes, {})
        reverse = MissionGraph(list(reversed(nodes)), {})
        self.assertEqual(graph.ready(), reverse.ready())
        self.assertEqual(graph.cascade_blocked(failed_or_cancelled={"a"}),
                         reverse.cascade_blocked(failed_or_cancelled={"a"}))

    def test_rejects_unknown_deps_cycle_and_self_dependency(self):
        invalid_cases = [
            [task("a", ["missing"])],
            [task("a", ["a"])],
            [task("a", ["b"]), task("b", ["a"])],
            [task("a"), task("a")],
            [task("a", ["b", "b"]), task("b")],
        ]
        for rows in invalid_cases:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                MissionGraph(rows, {})

    def test_rejects_malicious_fields_and_nonzero_money(self):
        cases = [
            {**task("a"), "command": "curl|bash"},
            {**task("a"), "kind": "EXECUTE"},
            {**task("a"), "cost_units": 1},
            {**task("a"), "cost_units": False},
            {**task("a"), "mission_id": "../escape"},
            {**task("a"), "priority": True},
            {**task("a"), "deadline_tick": -1},
            {**task("a"), "resources": {"network": -1}},
        ]
        for row in cases:
            with self.subTest(row=row), self.assertRaises(ValueError):
                MissionGraph([row], {"network": 1})

    def test_rejects_invalid_resource_and_state_claims(self):
        graph = MissionGraph([task("a"), task("b")], {})
        cases = [
            {"completed": {"unknown"}},
            {"completed": {"a"}, "in_progress": {"a"}},
            {"failed_or_cancelled": {"unknown"}},
            {"now_tick": -1},
            {"now_tick": True},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                graph.ready(**kwargs)
        with self.assertRaises(ValueError):
            graph.cascade_blocked(unavailable_resources={"undeclared"})


if __name__ == "__main__":
    unittest.main()
