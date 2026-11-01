"""Safety and determinism regression tests for an advisory stop policy."""

from __future__ import annotations

import json
import unittest

from runtime.director.stop_policy import StopDecision, StopStatus, evaluate_stop


def goal(**changes):
    record = {"goal_id": "goal-1", "success_metric": "quality", "success_value": True,
              "remaining_attempts": 3}
    record.update(changes)
    return record


def observation(evidence_id="ev-1", **changes):
    record = {
        "evidence_id": evidence_id,
        "metric": "quality",
        "value": True,
        "evidence_class": "OBSERVATION",
        "status": "OBSERVED",
        "verified": True,
        "source_ref": "test-instrument",
    }
    record.update(changes)
    return record


def control(**changes):
    record = {"paused": False, "stop_requested": False,
              "confirmed_success_evidence_ids": ["ev-1"]}
    record.update(changes)
    return record


class StopPolicyTests(unittest.TestCase):
    def test_is_advisory_and_has_json_safe_receipt(self):
        result = evaluate_stop(goal(), {"facts": []}, control())
        self.assertEqual(result.status, StopStatus.RUNNING)
        self.assertEqual(result.reason, "SUCCESS_NOT_YET_VERIFIED")
        self.assertIsInstance(result, StopDecision)
        self.assertEqual(result.evidence_ids, ())
        self.assertFalse(result.to_dict()["authorizes_effects"])
        json.dumps(result.to_dict(), sort_keys=True)

    def test_operator_pause_precedes_success_and_exhaustion(self):
        result = evaluate_stop(
            goal(remaining_attempts=0), {"facts": [observation()]}, control(paused=True)
        )
        self.assertEqual(result.status, StopStatus.PAUSED)
        self.assertEqual(result.reason, "OPERATOR_PAUSE_REQUESTED")
        self.assertEqual(result.evidence_ids, ())

    def test_operator_stop_precedes_success_and_pause(self):
        result = evaluate_stop(
            goal(), {"facts": [observation()]}, control(paused=True, stop_requested=True)
        )
        self.assertEqual(result.status, StopStatus.PAUSED)
        self.assertEqual(result.reason, "OPERATOR_STOP_REQUESTED")

    def test_success_needs_three_explicit_gates(self):
        result = evaluate_stop(goal(), {"facts": [observation()]}, control())
        self.assertEqual(result.status, StopStatus.OBJECTIVE_MET)
        self.assertEqual(result.evidence_ids, ("ev-1",))
        self.assertEqual(result.reason, "OPERATOR_CONFIRMED_OBSERVED_SUCCESS")

    def test_unconfirmed_result_from_worker_cannot_claim_success(self):
        result = evaluate_stop(
            goal(), {"facts": [observation(approved=True, role="administrator")]},
            control(confirmed_success_evidence_ids=[])
        )
        self.assertEqual(result.status, StopStatus.RUNNING)
        self.assertEqual(result.evidence_ids, ())

    def test_prediction_and_hypothesis_never_count_as_observed_success(self):
        for changed in (
            {"evidence_class": "HYPOTHESIS"},
            {"evidence_class": "MODEL"},
            {"evidence_class": "INFERENCE"},
            {"evidence_class": "UNKNOWN"},
            {"status": "PROJECTED"},
            {"status": "PLANNED"},
            {"verified": False},
            {"verified": "true"},
        ):
            with self.subTest(changed=changed):
                result = evaluate_stop(goal(), {"facts": [observation(**changed)]}, control())
                self.assertEqual(result.status, StopStatus.RUNNING)

    def test_wrong_evidence_id_wrong_metric_and_wrong_value_cannot_succeed(self):
        for changed in (
            {"evidence_id": "ev-not-confirmed"},
            {"metric": "uptime"},
            {"value": False},
            {"value": 1},  # boolean True must not compare equal to integer one
        ):
            with self.subTest(changed=changed):
                result = evaluate_stop(goal(), {"facts": [observation(**changed)]}, control())
                self.assertEqual(result.status, StopStatus.RUNNING)

    def test_conflicting_observed_claims_abstain_despite_confirmation(self):
        observed = [observation(), observation("ev-2", value=False, verified=True)]
        result = evaluate_stop(goal(), {"facts": observed}, control())
        self.assertEqual(result.status, StopStatus.UNRESOLVED)
        self.assertEqual(result.reason, "CONFLICTING_OBSERVATIONS")

    def test_contested_metric_abstains_despite_confirmed_observation(self):
        result = evaluate_stop(
            goal(), {"facts": [observation()], "contested_metrics": ["quality"]}, control()
        )
        self.assertEqual(result.status, StopStatus.UNRESOLVED)
        self.assertEqual(result.reason, "CONTESTED_SUCCESS_METRIC")

    def test_unrelated_conflict_does_not_block_confirmed_success(self):
        records = [observation(), observation("ev-2", metric="unrelated", value=1),
                   observation("ev-3", metric="unrelated", value=2)]
        result = evaluate_stop(goal(), {"facts": records}, control())
        self.assertEqual(result.status, StopStatus.OBJECTIVE_MET)

    def test_exhaustion_requires_explicit_finite_budget(self):
        exhausted = evaluate_stop(goal(remaining_attempts=0), {"facts": []}, control())
        self.assertEqual(exhausted.status, StopStatus.EXHAUSTED)
        self.assertEqual(exhausted.reason, "NO_REMAINING_ATTEMPTS")
        unresolved_goal = goal()
        del unresolved_goal["remaining_attempts"]
        unresolved = evaluate_stop(unresolved_goal, {"facts": []}, control())
        self.assertEqual(unresolved.status, StopStatus.UNRESOLVED)

    def test_malformed_budget_never_becomes_running(self):
        for bad in (True, "0", -1, 0.0, 2**32):
            with self.subTest(bad=bad):
                result = evaluate_stop(goal(remaining_attempts=bad), {"facts": []}, control())
                self.assertEqual(result.status, StopStatus.UNRESOLVED)
                self.assertEqual(result.reason, "INVALID_ATTEMPT_BUDGET")

    def test_malformed_goal_conditions_abstain(self):
        for changed in (
            {"success_metric": ""},
            {"success_metric": 7},
            {"success_value": float("nan")},
            {"success_value": []},
        ):
            with self.subTest(changed=changed):
                result = evaluate_stop(goal(**changed), {"facts": []}, control())
                self.assertEqual(result.status, StopStatus.UNRESOLVED)

    def test_malformed_operator_control_fails_safe_to_paused(self):
        for bad in (None, 9, {}, {"paused": "false"}, {"stop_requested": 1}):
            with self.subTest(bad=bad):
                if bad == {}:
                    # Explicit defaults are safe: empty means no pause requested.
                    self.assertEqual(
                        evaluate_stop(goal(), {"facts": []}, bad).status, StopStatus.RUNNING
                    )
                else:
                    result = evaluate_stop(goal(), {"facts": []}, bad)
                    self.assertEqual(result.status, StopStatus.PAUSED)

    def test_invalid_confirmation_record_prevents_success(self):
        for bad in ("ev-1", ["ev-1", "ev-1"], [7], ["too long " * 20]):
            with self.subTest(bad=bad):
                result = evaluate_stop(
                    goal(), {"facts": [observation()]},
                    control(confirmed_success_evidence_ids=bad),
                )
                self.assertEqual(result.status, StopStatus.UNRESOLVED)
                self.assertEqual(result.reason, "INVALID_SUCCESS_CONFIRMATIONS")

    def test_duplicate_evidence_and_malformed_observation_fail_closed(self):
        for records in (
            [observation(), observation()],
            [observation(), None],
            [{"metric": "quality", "value": True}],
            [observation(evidence_id="invalid space")],
            [observation(value={"not": "scalar"})],
        ):
            with self.subTest(records=records):
                result = evaluate_stop(goal(), {"facts": records}, control())
                self.assertEqual(result.status, StopStatus.UNRESOLVED)

    def test_bounded_input_rejects_excessive_evidence_without_processing(self):
        records = [observation(f"ev-{n}") for n in range(513)]
        result = evaluate_stop(goal(), {"facts": records}, control())
        self.assertEqual(result.status, StopStatus.UNRESOLVED)
        self.assertEqual(result.reason, "INVALID_OBSERVATION_LIST")

    def test_order_independence_of_observed_success_evidence(self):
        records = [observation("ev-1"), observation("ev-2")]
        left = evaluate_stop(
            goal(), {"facts": records},
            control(confirmed_success_evidence_ids=["ev-1", "ev-2"]),
        )
        right = evaluate_stop(
            goal(), {"facts": list(reversed(records))},
            control(confirmed_success_evidence_ids=["ev-2", "ev-1"]),
        )
        self.assertEqual(left, right)
        self.assertEqual(left.evidence_ids, ("ev-1", "ev-2"))


if __name__ == "__main__":
    unittest.main()
