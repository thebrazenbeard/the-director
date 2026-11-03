"""The data-only subprocess is never mistaken for an OS security sandbox."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from runtime.director.agent_runtime import IsolatedAgentRuntime, _ACTIVE_IDS


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
    def test_reader_start_refusal_cleans_reservation_without_masking_error(self):
        """A real Thread cannot be joined before its start was accepted."""
        fake_process = Mock()
        fake_process.wait.return_value = 0
        fake_process.stdin = Mock()
        fake_process.stdout = Mock()
        fake_working = Mock()
        fake_working.name = "unused-disposable-test-directory"
        fake_reader = Mock()
        fake_reader.start.side_effect = RuntimeError("original reader start refusal")
        fake_reader.join.side_effect = RuntimeError("cannot join thread before it is started")
        fake_reader.is_alive.return_value = False

        self.assertNotIn("agent.copy", _ACTIVE_IDS)
        with patch("runtime.director.agent_runtime.subprocess.Popen", return_value=fake_process), \
             patch("runtime.director.agent_runtime.tempfile.TemporaryDirectory", return_value=fake_working), \
             patch("runtime.director.agent_runtime.threading.Thread", return_value=fake_reader):
            with self.assertRaisesRegex(RuntimeError, "original reader start refusal"):
                IsolatedAgentRuntime.start(_record(), operator_approval=_approval())

        fake_process.terminate.assert_called_once()
        fake_process.wait.assert_called_once()
        fake_reader.join.assert_not_called()
        fake_working.cleanup.assert_called_once()
        self.assertNotIn("agent.copy", _ACTIVE_IDS)

    def test_startup_cleanup_secondary_failures_preserve_primary_error(self):
        failures = ("wait", "stdin", "stdout", "reader_status", "working")
        for failure in failures:
            with self.subTest(failure=failure):
                fake_process = Mock()
                fake_process.wait.return_value = 0
                fake_process.poll.return_value = 0  # confirmed exited despite any wait error
                fake_process.stdin = Mock()
                fake_process.stdout = Mock()
                fake_working = Mock()
                fake_working.name = "unused-disposable-test-directory"
                fake_reader = Mock()
                fake_reader.start.side_effect = RuntimeError("original reader start refusal")
                fake_reader.is_alive.return_value = False
                if failure == "wait":
                    fake_process.wait.side_effect = OSError("secondary wait error")
                if failure == "stdin":
                    fake_process.stdin.close.side_effect = OSError("secondary stdin error")
                if failure == "stdout":
                    fake_process.stdout.close.side_effect = OSError("secondary stdout error")
                if failure == "reader_status":
                    fake_reader.is_alive.side_effect = RuntimeError("secondary thread error")
                if failure == "working":
                    fake_working.cleanup.side_effect = OSError("secondary cleanup error")
                try:
                    with patch("runtime.director.agent_runtime.subprocess.Popen", return_value=fake_process), \
                         patch("runtime.director.agent_runtime.tempfile.TemporaryDirectory", return_value=fake_working), \
                         patch("runtime.director.agent_runtime.threading.Thread", return_value=fake_reader):
                        with self.assertRaisesRegex(RuntimeError, "original reader start refusal"):
                            IsolatedAgentRuntime.start(_record(), operator_approval=_approval())
                    fake_working.cleanup.assert_called_once()
                    self.assertNotIn("agent.copy", _ACTIVE_IDS)
                finally:
                    _ACTIVE_IDS.discard("agent.copy")  # reset failed synthetic source tests only

    def test_close_secondary_pipe_error_preserves_retry_and_releases_reservation(self):
        fake_process = Mock()
        fake_process.poll.return_value = 0  # process already confirmed exited
        fake_process.wait.return_value = 0
        fake_process.stdin.close.side_effect = OSError("secondary stdin close")
        fake_working = Mock()
        fake_reader = Mock()
        fake_reader.is_alive.return_value = False
        runtime = IsolatedAgentRuntime(fake_process, fake_working, "agent.copy",
                                       Mock(), fake_reader, 1.0)
        _ACTIVE_IDS.add("agent.copy")
        try:
            with self.assertRaisesRegex(RuntimeError, "cleanup incomplete"):
                runtime.close()
            fake_process.stdout.close.assert_called_once()
            fake_working.cleanup.assert_called_once()
            self.assertNotIn("agent.copy", _ACTIVE_IDS)
            fake_process.stdin.close.side_effect = None
            runtime.close()  # retry a previously failed cleanup, not a stale no-op
            self.assertNotIn("agent.copy", _ACTIVE_IDS)
        finally:
            _ACTIVE_IDS.discard("agent.copy")

    def test_start_refusal_with_unconfirmed_child_retains_conservative_id_hold(self):
        fake_process = Mock()
        fake_process.poll.return_value = None  # no observed exit
        fake_process.wait.side_effect = OSError("wait failed; no exit receipt")
        fake_working = Mock()
        fake_working.name = "unused-disposable-test-directory"
        fake_reader = Mock()
        fake_reader.start.side_effect = RuntimeError("original reader start refusal")
        fake_reader.is_alive.return_value = False
        try:
            with patch("runtime.director.agent_runtime.subprocess.Popen", return_value=fake_process), \
                 patch("runtime.director.agent_runtime.tempfile.TemporaryDirectory", return_value=fake_working), \
                 patch("runtime.director.agent_runtime.threading.Thread", return_value=fake_reader):
                with self.assertRaisesRegex(RuntimeError, "original reader start refusal") as raised:
                    IsolatedAgentRuntime.start(_record(), operator_approval=_approval())
            self.assertTrue(any("exit unverified" in note for note in raised.exception.__notes__))
            fake_working.cleanup.assert_called_once()
            self.assertIn("agent.copy", _ACTIVE_IDS)  # never imply the child stopped
        finally:
            _ACTIVE_IDS.discard("agent.copy")  # remove synthetic reservation only

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
