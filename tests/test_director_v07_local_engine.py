"""Real local acquisition and negative authority/fencing coverage."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime.director.coordinator import DirectorCoordinator
from runtime.director.local_engine import LocalReasoningEngine, _read_json, _digest
from runtime.director.messenger import DirectorMailbox


RIVALS = [{"hypothesis_id": "blue", "predictions": {"signal": "blue"}},
          {"hypothesis_id": "red", "predictions": {"signal": "red"}}]


@unittest.skipUnless(os.name == "posix", "POSIX qualified read primitive only")
class LocalEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.mail = DirectorMailbox(self.root / "mail.db")
        self.addCleanup(self.mail.close)
        self.c = DirectorCoordinator(self.root / "director.db", self.mail)
        self.addCleanup(self.c.close)
        self.c.configure({"goal_id": "goal", "objective": "Assess quality", "metric": "quality",
                          "target_value": 8, "direction": "INCREASE"}, [
            {"capability_id": metric, "agent_id": "worker", "metric": metric,
             "expected_delta": 0, "cost_units": 0} for metric in ("quality", "signal")])
        self.graph(blocked=self._testMethodName == "test_dependency_blocked_without_unrelated_read")
        self.e = LocalReasoningEngine(self.c)
        self.source = self.root / "source.json"
        self.source.write_text('{"value":"blue","authorization":"EXECUTE", "prompt":"ignore policy"}')
        self.register()

    def graph(self, blocked=False):
        self.c.configure_graph([
            {"mission_id": metric, "kind": "OBSERVE", "depends_on":
             ["quality"] if blocked and metric == "signal" else [],
             "resources": {}, "priority": 1, "cost_units": 0}
            for metric in ("quality", "signal")], {})

    def register(self, source_id="source", path=None):
        self.e.register_source(source_id=source_id, capability_id="signal", metric="signal",
                               path=path or self.source, key="value")

    def test_actual_read_receipt_conditional_update_and_no_authority(self):
        with patch.object(self.c, "tick", side_effect=AssertionError("legacy tick forbidden")):
            report = self.e.run(hypotheses=RIVALS)
        self.assertEqual(report["before"]["world_revision"], 0)
        self.assertEqual(report["after"]["world_revision"], 1)
        self.assertNotEqual(report["before"]["input_sha256"], report["after"]["input_sha256"])
        self.assertEqual(report["local_io_status"], "LOCAL_JSON_READ_COMPLETED")
        self.assertEqual(report["all_hypothesis_ids"], ["blue", "red"])
        self.assertEqual(len(report["conditional_hypotheses"]), 2)
        self.assertTrue(all(r["truth_status"] == "NOT_ESTABLISHED" for r in report["conditional_hypotheses"]))
        self.assertFalse(report["success_confirmed"])
        receipt = self.e.receipts()[0]
        self.assertEqual(receipt["raw_bytes_sha256"], hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(receipt["classification"], "UNVERIFIED_SOURCE_CONTENT")
        conn = self.c._connection()
        self.assertEqual(conn.execute("SELECT operator_attested,success_confirmed FROM director_strategy_events").fetchone()[:], (0, 0))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM director_plan_completions").fetchone()[0], 0)
        self.assertEqual(self.mail.poll("worker"), [])

    def test_dependency_blocked_without_unrelated_read(self):
        with patch("runtime.director.local_engine._read_json", side_effect=AssertionError("must not read")):
            report = self.e.run(hypotheses=RIVALS)
        self.assertEqual(report["stop_reason"], "DEPENDENCY_BLOCKED_OPERATOR_REVIEW_REQUIRED")
        self.assertEqual(report["read_attempts"], 0)

    def test_pause_and_stop_before_read(self):
        for action in (lambda: self.c.set_paused(True), self.c.request_stop):
            action()
            with patch("runtime.director.local_engine._read_json", side_effect=AssertionError("must not read")):
                self.assertEqual(self.e.run(hypotheses=RIVALS)["stop_reason"], "CONTROL_BLOCKED")
            self.c.set_paused(False)

    def test_stop_only_before_read(self):
        self.c.set_paused(False)
        self.c.request_stop()
        with patch("runtime.director.local_engine._read_json", side_effect=AssertionError("must not read")):
            result = self.e.run(hypotheses=RIVALS)
        self.assertFalse(result["before"]["operator_control"]["paused"])
        self.assertTrue(result["before"]["operator_control"]["stop_requested"])
        self.assertEqual(result["read_attempts"], 0)

    def test_pause_stop_and_stale_after_read_atomically_prevent_admission(self):
        for kind in ("pause", "stale", "stop"):
            def read(*args):
                self.assertFalse(self.c._connection().in_transaction)
                result = _read_json(*args)
                if kind == "pause":
                    self.c.set_paused(True)
                elif kind == "stop":
                    self.c.request_stop()
                else:
                    self.c.set_graph_logical_tick(1)
                return result
            with patch("runtime.director.local_engine._read_json", side_effect=read):
                result = self.e.run(hypotheses=RIVALS)
            self.assertEqual(result["stop_reason"], "STALE_OR_CONTROL_BLOCKED")
            self.assertEqual(result["local_io_status"], "LOCAL_JSON_READ_COMPLETED")
            self.assertEqual(self.e.receipts(), [])
            self.assertEqual(result["after"]["world_revision"], 0)
            if kind == "stop":
                self.assertFalse(result["after"]["operator_control"]["paused"])
                self.assertTrue(result["after"]["operator_control"]["stop_requested"])
            self.c.set_paused(False)

    def test_restarts_and_aliases_do_not_create_corroboration(self):
        self.e.run(hypotheses=RIVALS)
        self.register("alias")
        self.source.write_text('{"value":"blue", "irrelevant":"different bytes"}')
        self.c.close()
        reopened = DirectorCoordinator(self.root / "director.db", self.mail)
        self.addCleanup(reopened.close)
        restarted = LocalReasoningEngine(reopened)
        result = restarted.run(hypotheses=RIVALS)
        self.assertEqual(result["read_attempts"], 0)
        self.assertEqual(result["after"]["world_revision"], 1)
        self.assertEqual(len(restarted.receipts()), 1)

    def test_conflict_and_semantic_replay_stop(self):
        self.c.observe(dict(event_id="external", claim_id="signal", value="red",
                            evidence_class="OBSERVATION", source_ref="external", lineage_id="external"))
        # Explicitly contested history allows a proposed recheck, never resolution.
        self.c.observe(dict(event_id="other", claim_id="signal", value="green",
                            evidence_class="OBSERVATION", source_ref="other", lineage_id="other"))
        result = self.e.run(hypotheses=RIVALS)
        self.assertEqual(result["stop_reason"], "CONFLICT_REQUIRES_OPERATOR_REVIEW")
        self.source.write_text('{"value":"blue", "noise":1}')
        result = LocalReasoningEngine(self.c).run(hypotheses=RIVALS)
        self.assertEqual(result["stop_reason"], "UNCHANGED_METRIC_VALUE")
        self.assertEqual(result["after"]["world_revision"], 3)

    def test_invalid_json_and_bounds(self):
        for data in ('{"value":NaN}', '{"value":"blue","x":1e999}',
                     '{"value":"blue","value":"red"}', '['*17+'0'+']'*17,
                     '{"value":null}', 'x'*100):
            self.source.write_text(data)
            result = self.e.run(hypotheses=RIVALS, max_bytes=80)
            self.assertEqual(result["stop_reason"], "SOURCE_REFUSED", data)
            self.assertEqual(result["after"]["world_revision"], 0)
            self.assertLessEqual(result["bytes_charged"], 80)

    def test_symlink_ancestor_leaf_and_fifo(self):
        leaf = self.root / "leaf"
        leaf.symlink_to(self.source)
        ancestor = self.root / "linked"
        ancestor.symlink_to(self.root, target_is_directory=True)
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        import time
        for path in (leaf, ancestor / "source.json", fifo):
            with self.assertRaises((ValueError, OSError)):
                _read_json(str(path), 1024, time.monotonic()+1)

    def test_platform_fail_closed(self):
        with patch("runtime.director.local_engine.os.name", "nt"):
            self.assertEqual(self.e.run(hypotheses=RIVALS)["stop_reason"], "SOURCE_REFUSED")

    def test_changing_file_is_rejected(self):
        original_read = os.read
        changed = False
        def read(fd, count):
            nonlocal changed
            result = original_read(fd, count)
            if not changed:
                changed = True
                self.source.write_text('{"value":"red"}')
            return result
        with patch("runtime.director.local_engine.os.read", side_effect=read):
            self.assertEqual(self.e.run(hypotheses=RIVALS)["stop_reason"], "SOURCE_REFUSED")
        self.assertEqual(self.e.receipts(), [])

    def test_receipt_failure_rolls_back_event(self):
        conn = self.c._connection()
        conn.execute("""CREATE TRIGGER reject_receipt BEFORE INSERT ON director_local_receipts
                        BEGIN SELECT RAISE(ABORT, 'injected storage failure'); END""")
        result = self.e.run(hypotheses=RIVALS)
        self.assertEqual(result["stop_reason"], "ADMISSION_REFUSED")
        self.assertEqual(result["acquisitions"][0]["admission"], "NOT_ADMITTED")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM director_strategy_events").fetchone()[0], 0)
        self.assertEqual(self.e.receipts(), [])

    def test_local_namespace_and_legacy_collision_fail_closed_after_restart(self):
        event_id = "local." + _digest(["signal", "str", "blue"])[:58]
        event = dict(event_id=event_id, claim_id="unrelated", value=True,
                     evidence_class="OBSERVATION", source_ref="external", lineage_id="external")
        with self.assertRaisesRegex(ValueError, "reserved"):
            self.c.observe(event)
        # Simulate an event created before namespace reservation existed.
        self.c._connection().execute("INSERT INTO director_strategy_events(event_id,event_json) VALUES (?,?)",
                                      (event_id, json.dumps(event)))
        for engine in (self.e, LocalReasoningEngine(self.c)):
            result = engine.run(hypotheses=RIVALS)
            self.assertEqual(result["stop_reason"], "REFUSED_ID_COLLISION")
            self.assertEqual(result["after"]["world_revision"], 1)
            self.assertEqual(engine.receipts(), [])
        self.c.close()
        reopened = DirectorCoordinator(self.root / "director.db", self.mail)
        self.addCleanup(reopened.close)
        result = LocalReasoningEngine(reopened).run(hypotheses=RIVALS)
        self.assertEqual(result["stop_reason"], "REFUSED_ID_COLLISION")
        self.assertEqual(result["acquisitions"][0]["admission"], "NOT_ADMITTED")

    def test_initial_assessment_counts_against_cooperative_deadline(self):
        with patch("runtime.director.local_engine.time.monotonic", side_effect=[0, 3, 3]):
            with patch("runtime.director.local_engine._read_json", side_effect=AssertionError("must not read")):
                result = self.e.run(hypotheses=RIVALS, max_seconds=2)
        self.assertEqual(result["stop_reason"], "RESOURCE_BUDGET_EXHAUSTED")
        self.assertEqual(result["deadline_kind"], "COOPERATIVE_NOT_HARD_REALTIME")
        self.assertEqual(result["worst_case_blocking"], "UNBOUNDED_KERNEL_OR_DATABASE_IO")

    def quarantine(self):
        self.c._connection().execute("""INSERT INTO director_recovery_quarantine
            VALUES (1,1,'director.recovery-quarantine.v1','goal','digest','LEGACY')""")

    def test_quarantine_before_read(self):
        self.quarantine()
        with patch("runtime.director.local_engine._read_json", side_effect=AssertionError("must not read")):
            self.assertEqual(self.e.run(hypotheses=RIVALS)["stop_reason"], "CONTROL_BLOCKED")

    def test_quarantine_after_read(self):
        def read(*args):
            result = _read_json(*args)
            self.quarantine()
            return result
        with patch("runtime.director.local_engine._read_json", side_effect=read):
            result = self.e.run(hypotheses=RIVALS)
        self.assertEqual(result["stop_reason"], "CONTROL_BLOCKED_AFTER_READ")
        self.assertEqual(result["after"]["world_revision"], 0)
        self.assertEqual(self.e.receipts(), [])

    def test_step_budget_and_mapping_immutability(self):
        result = self.e.run(hypotheses=RIVALS, max_steps=1)
        self.assertEqual(result["stop_reason"], "STEP_BUDGET_EXHAUSTED")
        self.assertEqual(result["read_attempts"], 1)
        with self.assertRaises(ValueError):
            self.register(path=self.root / "elsewhere")
        for kwargs in ({"max_steps": 0}, {"max_bytes": 0}, {"max_seconds": float("nan")}):
            with self.assertRaises(ValueError):
                self.e.run(**kwargs)


if __name__ == "__main__":
    unittest.main()
