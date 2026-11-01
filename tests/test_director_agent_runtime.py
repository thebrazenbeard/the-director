"""End-to-end tests for the consent-started, data-only child process.

The fixed worker is deliberately not an interpreter for imported user code.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from runtime.director.agent_runtime import IsolatedAgentRuntime
from runtime.director.agent_state import AgentStateLedger, export_agent_state, restore_agent_state


def _state(*, memories=None):
    return {
        "goal": {
            "goal_id": "quality_goal",
            "objective": "Evaluate an illustrative metric",
            "metric": "quality",
            "target_value": 8,
            "direction": "INCREASE",
        },
        "memories": [] if memories is None else memories,
    }


def _consent(*, allow_memories=False):
    return {
        "approved": True,
        "agent_id": "analyst",
        "source_instance_id": "analyst.old",
        "target_domain": "local.domain",
        "allow_memories": allow_memories,
        "operator_ref": "operator.local",
    }


def _approval():
    return {
        "approved": True,
        "agent_id": "analyst",
        "new_instance_id": "analyst.new",
        "target_domain": "local.domain",
    }


def _runtime_approval(**changes):
    return {
        **_approval(),
        "purpose": "DATA_ONLY_LOCAL_RUNTIME",
        **changes,
    }


class IsolatedRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="director-runtime-test-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.ledger = AgentStateLedger(self.path / "used.sqlite3")
        self.addCleanup(self.ledger.close)

    def _restored(self, *, memories=None, allow_memories=False):
        capsule = export_agent_state(
            capsule_id="capsule.runtime.1", agent_id="analyst",
            source_instance_id="analyst.old", target_domain="local.domain",
            state=_state(memories=memories),
            consent=_consent(allow_memories=allow_memories),
        )
        return restore_agent_state(
            capsule, target_domain="local.domain", new_instance_id="analyst.new",
            operator_approval=_approval(), ledger=self.ledger,
        )

    def test_real_capsule_restore_starts_child_and_thinks_from_observations(self):
        restored = self._restored()
        with IsolatedAgentRuntime.start(restored, operator_approval=_runtime_approval()) as runtime:
            self.assertNotEqual(runtime.process.pid, os.getpid())
            initial = runtime.status()
            self.assertEqual(initial["instance_id"], "analyst.new")
            self.assertEqual(initial["restored_from_instance_id"], "analyst.old")
            self.assertEqual(initial["capsule_id"], "capsule.runtime.1")
            self.assertEqual(initial["target_domain"], "local.domain")
            self.assertEqual(initial["state"], "RUNNING")
            self.assertEqual(initial["goal_id"], "quality_goal")
            self.assertEqual(initial["memory_count"], 0)
            self.assertEqual(runtime.think()["assessment"], "NEEDS_DECLARED_OBSERVATION")

            result = runtime.observe(observation_id="obs1", metric="quality", value=3)
            self.assertEqual(result["observations"], 1)
            decision = runtime.think()
            self.assertEqual(decision["assessment"], "MORE_ANALYSIS_REQUIRED")
            self.assertEqual(decision["remaining_gap"], 5)
            self.assertEqual(decision["evidence_status"], "DECLARED_UNVERIFIED")
            self.assertEqual(decision["effects"], "NOT_EXECUTED")

            runtime.observe(observation_id="obs2", metric="quality", value=9)
            later = runtime.think()
            self.assertEqual(later["assessment"], "DECLARED_TARGET_REACHED_UNVERIFIED")
            self.assertFalse(later["objective_verified"])
        self.assertIsNotNone(runtime.process.poll())
        with self.assertRaises(RuntimeError):
            runtime.status()

    def test_requires_exact_target_bound_new_runtime_consent(self):
        restored = self._restored()
        for invalid in (
            _runtime_approval(approved=False),
            _runtime_approval(new_instance_id="someone.else"),
            _runtime_approval(target_domain="other.domain"),
            _runtime_approval(purpose="RUN_ARBITRARY_PYTHON"),
            {**_runtime_approval(), "shell": "powershell"},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    IsolatedAgentRuntime.start(restored, operator_approval=invalid)

    def test_unexpected_code_or_authority_fields_rejected(self):
        restored = self._restored()
        for tampered in (
            {**restored, "code": "print('run me')"},
            {**restored, "authority": "GRANTED"},
            {**restored, "subject_kind": "HUMAN"},
            {**restored, "state": {**restored["state"], "program": "import os"}},
        ):
            with self.subTest(key=next(iter(tampered))):
                with self.assertRaises(ValueError):
                    IsolatedAgentRuntime.start(tampered, operator_approval=_runtime_approval())

    def test_opted_in_memory_is_data_not_code_or_output(self):
        malicious = "__import__('os').system('echo should-not-run')"
        restored = self._restored(memories=[{"memory_id": "m1", "summary": malicious}],
                                  allow_memories=True)
        with IsolatedAgentRuntime.start(restored, operator_approval=_runtime_approval()) as runtime:
            self.assertEqual(runtime.status()["memory_count"], 1)
            self.assertNotIn(malicious, str(runtime.status()))
            self.assertNotIn(malicious, str(runtime.think()))

    def test_opted_in_memory_is_retained_as_inert_retrievable_state(self):
        restored = self._restored(memories=[
            {"memory_id": "quality.note", "summary": "The quality metric was measured locally."},
            {"memory_id": "other.note", "summary": "Weather is unavailable in this runtime."},
        ], allow_memories=True)
        with IsolatedAgentRuntime.start(restored, operator_approval=_runtime_approval()) as runtime:
            self.assertEqual(runtime.status()["memory_count"], 2)
            result = runtime.think()
            self.assertEqual(result["relevant_memory_ids"], ["quality.note"])
            self.assertNotIn("The quality metric", str(result))

    def test_step_limit_and_observation_conflicts_fail_closed(self):
        restored = self._restored()
        with IsolatedAgentRuntime.start(restored, operator_approval=_runtime_approval(), max_steps=2) as runtime:
            runtime.observe(observation_id="obs1", metric="quality", value=3)
            self.assertEqual(runtime.observe(observation_id="obs1", metric="quality", value=3)["observations"], 1)
            with self.assertRaises(ValueError):
                runtime.observe(observation_id="obs1", metric="quality", value=4)
            runtime.think()
            with self.assertRaises(RuntimeError):
                runtime.think()

    def test_missing_or_mismatched_metric_does_not_change_state(self):
        restored = self._restored()
        with IsolatedAgentRuntime.start(restored, operator_approval=_runtime_approval()) as runtime:
            with self.assertRaises(ValueError):
                runtime.observe(observation_id="other", metric="unrelated", value=7)
            self.assertEqual(runtime.status()["observation_count"], 0)
            self.assertEqual(runtime.think()["assessment"], "NEEDS_DECLARED_OBSERVATION")

    def test_same_instance_cannot_be_started_twice_in_parent(self):
        restored = self._restored()
        with IsolatedAgentRuntime.start(restored, operator_approval=_runtime_approval()):
            with self.assertRaises(ValueError):
                IsolatedAgentRuntime.start(restored, operator_approval=_runtime_approval())
        with IsolatedAgentRuntime.start(restored, operator_approval=_runtime_approval()) as runtime:
            self.assertEqual(runtime.status()["state"], "RUNNING")

    def test_child_exit_does_not_leave_hanging_or_external_effects(self):
        restored = self._restored()
        runtime = IsolatedAgentRuntime.start(restored, operator_approval=_runtime_approval())
        self.assertEqual(runtime.status()["state"], "RUNNING")
        runtime.process.terminate()
        runtime.process.wait(timeout=5)
        with self.assertRaises(RuntimeError):
            runtime.status()
        runtime.close()
        runtime.close()

    def test_input_bounds_and_no_arbitrary_ops(self):
        restored = self._restored()
        with self.assertRaises(ValueError):
            IsolatedAgentRuntime.start(restored, operator_approval=_runtime_approval(), max_steps=0)
        with IsolatedAgentRuntime.start(restored, operator_approval=_runtime_approval()) as runtime:
            with self.assertRaises(ValueError):
                runtime.observe(observation_id="x" * 65, metric="quality", value=1)
            with self.assertRaises(ValueError):
                runtime.observe(observation_id="valid", metric="quality", value=float("nan"))
            with self.assertRaises(ValueError):
                runtime._request({"op": "RUN_CODE", "code": "print(1)"})


if __name__ == "__main__":
    unittest.main()
