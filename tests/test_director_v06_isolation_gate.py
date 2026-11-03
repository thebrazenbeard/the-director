"""The data-only subprocess is never mistaken for an OS security sandbox."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from runtime.director.agent_runtime import IsolatedAgentRuntime


def _record():
    return {
        "new_instance_id": "agent.copy", "agent_id": "analyst",
        "restored_from_instance_id": "agent.source", "capsule_id": "capsule.1",
        "target_domain": "local.domain", "subject_kind": "SOFTWARE_AGENT",
        "state": {"goal": {"goal_id": "goal.a", "objective": "Assess a declared metric",
                           "metric": "quality", "target_value": 8, "direction": "INCREASE"},
                  "memories": []},
        "memory_manifest": {"input_count": 0, "transferred_count": 0,
                            "omitted_count": 0},
        "authority": "NOT_GRANTED", "effects": "NOT_EXECUTED",
        "continuity_claim": "INFORMATION_STATE_COPY_ONLY",
    }


def _approval():
    return {"approved": True, "agent_id": "analyst", "new_instance_id": "agent.copy",
            "target_domain": "local.domain", "purpose": "DATA_ONLY_LOCAL_RUNTIME"}


class IsolationGateTests(unittest.TestCase):
    def test_default_remains_data_only_process_with_explicit_boundary_status(self):
        with IsolatedAgentRuntime.start(_record(), operator_approval=_approval()) as runtime:
            status = runtime.status()
            self.assertEqual(status["execution_mode"], "FIXED_DATA_ONLY_PROTOCOL")
            self.assertEqual(status["os_principal_isolation_status"], "NOT_VERIFIED")
            self.assertEqual(status["isolation_mechanism"], "SAME_ACCOUNT_SUBPROCESS")
            self.assertEqual(status["host_effects"], "NOT_EXECUTED")
            self.assertEqual(runtime.think()["assessment"], "NEEDS_DECLARED_OBSERVATION")

    def test_strict_requirement_rejects_before_process_or_tempfile(self):
        with patch("runtime.director.agent_runtime.subprocess.Popen") as spawn, \
             patch("runtime.director.agent_runtime.tempfile.TemporaryDirectory") as temp:
            with self.assertRaisesRegex(RuntimeError, "OS-principal isolation.*unavailable"):
                IsolatedAgentRuntime.start(_record(), operator_approval=_approval(),
                                           require_os_principal_isolation=True)
            spawn.assert_not_called()
            temp.assert_not_called()

    def test_strict_requirement_has_no_silent_fallback_and_rejects_non_bool(self):
        for invalid in (None, 0, 1, "true", "false", [], {}):
            with self.subTest(invalid=invalid):
                with patch("runtime.director.agent_runtime.subprocess.Popen") as spawn:
                    with self.assertRaisesRegex(ValueError, "boolean"):
                        IsolatedAgentRuntime.start(_record(), operator_approval=_approval(),
                                                   require_os_principal_isolation=invalid)
                    spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
