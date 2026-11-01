"""Tests for the no-effect, zero-cost classical strategic mission generator."""

import copy
import json
import unittest

from runtime.director.strategy import generate_candidates


def goal(direction="INCREASE", target=10):
    return {
        "goal_id": "g.one",
        "objective": "Improve an observed diagnostic metric",
        "metric": "readiness",
        "target_value": target,
        "direction": direction,
    }


def capabilities():
    return [
        {
            "capability_id": "diagnose_fast",
            "agent_id": "analyst.a",
            "metric": "readiness",
            "expected_delta": 6,
            "cost_units": 0,
        },
        {
            "capability_id": "diagnose_precise",
            "agent_id": "analyst.b",
            "metric": "readiness",
            "expected_delta": 1,
            "cost_units": 0,
        },
        {
            "capability_id": "other_metric",
            "agent_id": "analyst.c",
            "metric": "something_else",
            "expected_delta": 9,
            "cost_units": 0,
        },
    ]


def world(value=3, status="OBSERVED", revision=1):
    return {
        "revision": revision,
        "facts": {
            "readiness": {
                "value": value,
                "status": status,
                "evidence_ids": ["e.one"],
            }
        },
        "events": [],
    }


class StrategicGenerationTests(unittest.TestCase):
    def test_goal_and_capabilities_generate_missions_without_supplied_proposals(self):
        result = generate_candidates(goal(), capabilities(), world())
        self.assertEqual([r["capability_id"] for r in result], [
            "diagnose_fast", "diagnose_precise"
        ])
        self.assertEqual([r["assigned_agent_id"] for r in result], [
            "analyst.a", "analyst.b"
        ])
        self.assertEqual(result[0]["projected_value"], 9)
        self.assertEqual(result[0]["progress_points"], 6)
        self.assertEqual(result[0]["goal_id"], "g.one")
        self.assertEqual(result[0]["plan_revision"], 1)

    def test_every_mission_is_unexecuted_simulated_and_not_authorized(self):
        result = generate_candidates(goal(), capabilities(), world())
        for item in result:
            self.assertEqual(item["model_status"], "SIMULATED_UNVERIFIED")
            self.assertEqual(item["input_integrity"], "DECLARED_UNVERIFIED")
            self.assertEqual(item["effect_status"], "NOT_EXECUTED")
            self.assertTrue(item["requires_human_authorization"])
            self.assertEqual(item["observation_status"], "OBSERVED")
            self.assertEqual(item["kind"], "PLAN")
            self.assertTrue(item["mission_id"].startswith("dr-"))
        json.dumps(result)

    def test_replan_after_observed_metric_change_and_revision(self):
        initial = generate_candidates(goal(), capabilities(), world(value=3, revision=5))
        updated = generate_candidates(goal(), capabilities(), world(value=9, revision=6))
        self.assertEqual(initial[0]["capability_id"], "diagnose_fast")
        # When only one point is missing, the larger modelled jump would
        # overshoot; the smaller, more precise analytical option ranks first.
        self.assertEqual(updated[0]["capability_id"], "diagnose_precise")
        self.assertNotEqual(initial[0]["mission_id"], updated[0]["mission_id"])
        self.assertEqual(updated[0]["projected_value"], 10)

    def test_mission_identity_changes_with_material_goal_or_capability_change(self):
        original = generate_candidates(goal(), capabilities()[:1], world())[0]["mission_id"]
        altered_goal = generate_candidates(goal(target=11), capabilities()[:1], world())[0]["mission_id"]
        revised_agent = capabilities()[:1]
        revised_agent[0]["agent_id"] = "different.analyst"
        altered_agent = generate_candidates(goal(), revised_agent, world())[0]["mission_id"]
        revised_model = capabilities()[:1]
        revised_model[0]["expected_delta"] = 4
        altered_projection = generate_candidates(goal(), revised_model, world())[0]["mission_id"]
        self.assertEqual(len({original, altered_goal, altered_agent, altered_projection}), 4)

    def test_goal_reached_produces_no_missions(self):
        self.assertEqual(generate_candidates(goal(), capabilities(), world(value=10)), [])
        self.assertEqual(generate_candidates(goal(), capabilities(), world(value=15)), [])
        self.assertEqual(
            generate_candidates(goal(direction="DECREASE", target=3), capabilities(), world(value=2)), []
        )

    def test_missing_metric_generates_observation_not_imaginary_forecast(self):
        result = generate_candidates(goal(), capabilities(), {"revision": 3, "facts": {}})
        self.assertEqual(len(result), 2)
        for item in result:
            self.assertEqual(item["kind"], "OBSERVE")
            self.assertEqual(item["observation_status"], "UNKNOWN")
            self.assertIsNone(item["observed_value"])
            self.assertIsNone(item["expected_delta"])
            self.assertIsNone(item["projected_value"])
            self.assertIsNone(item["progress_points"])

    def test_contested_evidence_results_in_verification_only(self):
        result = generate_candidates(goal(), capabilities(), world(value=None, status="CONTESTED"))
        self.assertEqual(len(result), 2)
        self.assertTrue(all(item["kind"] == "VERIFY" for item in result))
        self.assertTrue(all(item["projected_value"] is None for item in result))

    def test_hypothesis_and_inference_do_not_become_observed(self):
        for status in ("INFERRED", "HYPOTHETICAL", "UNKNOWN"):
            with self.subTest(status=status):
                result = generate_candidates(goal(), capabilities(), world(value=1000, status=status))
                self.assertTrue(result)
                self.assertTrue(all(row["kind"] == "OBSERVE" for row in result))
                self.assertTrue(all(row["observed_value"] is None for row in result))

    def test_deterministic_across_capability_order_and_input_dictionaries(self):
        baseline = generate_candidates(goal(), capabilities(), world())
        reversed_inputs = generate_candidates(dict(reversed(list(goal().items()))), list(reversed(capabilities())), world())
        self.assertEqual(baseline, reversed_inputs)
        again = generate_candidates(goal(), capabilities(), world())
        self.assertEqual(baseline, again)

    def test_input_unmodified_and_result_is_independent(self):
        raw_goal, raw_caps, raw_world = goal(), capabilities(), world()
        orig = copy.deepcopy((raw_goal, raw_caps, raw_world))
        result = generate_candidates(raw_goal, raw_caps, raw_world)
        result[0]["rationale"] = "changed by caller"
        self.assertEqual((raw_goal, raw_caps, raw_world), orig)

    def test_limit_and_mismatched_catalog(self):
        result = generate_candidates(goal(), capabilities(), world(), max_candidates=1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["capability_id"], "diagnose_fast")
        self.assertEqual(generate_candidates(goal(), capabilities()[2:], world()), [])
        self.assertEqual(generate_candidates(goal(), [], world()), [])

    def test_decrease_goal_only_keeps_directionally_sensible_candidates(self):
        caps = capabilities()
        caps[0]["expected_delta"] = -3
        caps[1]["expected_delta"] = +1
        result = generate_candidates(goal(direction="DECREASE", target=1), caps, world(value=6))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["capability_id"], "diagnose_fast")
        self.assertEqual(result[0]["projected_value"], 3)

    def test_zero_delta_is_analysis_not_progress(self):
        cap = capabilities()[0]
        cap["expected_delta"] = 0
        result = generate_candidates(goal(), [cap], world())
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["kind"], "ANALYZE")
        self.assertEqual(result[0]["progress_points"], 0)

    def test_cost_is_hard_zero_budget(self):
        cap = capabilities()[0]
        cap["cost_units"] = 1
        with self.assertRaisesRegex(ValueError, "zero-cost"):
            generate_candidates(goal(), [cap], world())

    def test_invalid_numeric_inputs_fail_closed(self):
        for value in (True, float("nan"), float("inf"), -float("inf"), 10**100, "5"):
            with self.subTest(value=repr(value)):
                g = goal()
                g["target_value"] = value
                with self.assertRaises(ValueError):
                    generate_candidates(g, capabilities(), world())
        for value in (None, False, float("nan"), 10**100):
            with self.subTest(current=repr(value)):
                with self.assertRaises(ValueError):
                    generate_candidates(goal(), capabilities(), world(value=value))

    def test_invalid_catalog_entries_fail_closed(self):
        cases = [
            {**capabilities()[0], "agent_id": "bad id"},
            {**capabilities()[0], "extra_command": "shell"},
            {**capabilities()[0], "expected_delta": float("nan")},
            {**capabilities()[0], "cost_units": False},
            {**capabilities()[0], "description": "line\nbreak"},
        ]
        for entry in cases:
            with self.subTest(entry=repr(entry)):
                with self.assertRaises(ValueError):
                    generate_candidates(goal(), [entry], world())

    def test_duplicate_capability_identity_rejected(self):
        cap = capabilities()[0]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            generate_candidates(goal(), [cap, dict(cap)], world())

    def test_invalid_goal_extra_field_rejected(self):
        g = goal()
        g["execution_permission"] = True
        with self.assertRaises(ValueError):
            generate_candidates(g, capabilities(), world())

    def test_bad_world_inputs_fail_closed(self):
        invalid_worlds = [
            {},
            {"revision": False, "facts": {}},
            {"revision": -1, "facts": {}},
            {"revision": 1, "facts": []},
            world(status="OFFICIAL"),
            {"revision": 1, "facts": {"readiness": 9}},
        ]
        for snapshot in invalid_worlds:
            with self.subTest(snapshot=snapshot):
                with self.assertRaises(ValueError):
                    generate_candidates(goal(), capabilities(), snapshot)

    def test_invalid_bounds(self):
        for n in (0, 33, True, 1.2):
            with self.subTest(n=n):
                with self.assertRaises(ValueError):
                    generate_candidates(goal(), capabilities(), world(), max_candidates=n)
        with self.assertRaises(ValueError):
            generate_candidates(goal(), capabilities() * 50, world())


if __name__ == "__main__":
    unittest.main()
