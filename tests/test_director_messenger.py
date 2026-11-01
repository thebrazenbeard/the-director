"""Adversarial local-only tests for typed Director message delivery."""

from __future__ import annotations

import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from runtime.director.messenger import DirectorMailbox, MessageConflict


class DirectorMailboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "messages.sqlite3"
        self.box = DirectorMailbox(self.path)
        self.addCleanup(self.box.close)

    def send(self, message_id="mission-001", **overrides):
        values = {
            "sender_id": "director",
            "recipient_id": "analyst_01",
            "kind": "MISSION_ASSIGNMENT",
            "payload": {"goal_id": "goal-a", "tasks": ["review evidence"]},
            "message_id": message_id,
        }
        values.update(overrides)
        return self.box.send(**values)

    def test_exact_recipient_delivery_order_and_cursor(self):
        first = self.send("mission-001")
        second = self.send("mission-002", recipient_id="analyst_02")
        third = self.send("mission-003", kind="STATUS")
        self.assertEqual([m["message_id"] for m in self.box.poll("analyst_01")], [
            "mission-001", "mission-003",
        ])
        self.assertEqual([m["message_id"] for m in self.box.poll("analyst_02")], [
            "mission-002",
        ])
        self.assertGreater(second["seq"], first["seq"])
        self.assertGreater(third["seq"], second["seq"])
        self.assertEqual([m["message_id"] for m in self.box.poll(
            "analyst_01", after_seq=first["seq"]
        )], ["mission-003"])
        self.assertEqual(self.box.poll("unknown"), [])

    def test_ids_are_idempotent_and_conflicts_cannot_overwrite(self):
        original = self.send()
        self.assertEqual(self.send(), original)
        with self.assertRaises(MessageConflict):
            self.send(payload={"goal_id": "substituted"})
        with self.assertRaises(MessageConflict):
            self.send(kind="RESULT")
        with self.assertRaises(MessageConflict):
            self.send(sender_id="elsewhere")
        with self.assertRaises(MessageConflict):
            self.send(recipient_id="other")
        self.assertEqual(self.box.poll("analyst_01"), [original])

    def test_equivalent_json_payload_order_is_idempotent(self):
        original = self.send(payload={"b": 2, "a": 1})
        identical = self.send(payload={"a": 1, "b": 2})
        self.assertEqual(identical, original)

    def test_generated_message_ids_have_independent_identity(self):
        first = self.send(message_id=None)
        second = self.send(message_id=None)
        self.assertNotEqual(first["message_id"], second["message_id"])
        self.assertEqual(first["correlation_id"], first["message_id"])

    def test_ack_is_durable_recipient_scoped_and_idempotent(self):
        original = self.send()
        with self.assertRaises(KeyError):
            self.box.ack("another_worker", original["message_id"])
        self.assertEqual(self.box.poll("analyst_01"), [original])
        acked = self.box.ack("analyst_01", original["message_id"])
        self.assertTrue(acked["acked"])
        self.assertIsNotNone(acked["acked_at_utc"])
        self.assertEqual(acked, self.box.ack("analyst_01", original["message_id"]))
        self.assertEqual(self.box.poll("analyst_01"), [])
        self.assertEqual(self.box.poll("analyst_01", include_acked=True), [acked])

    def test_reply_routes_back_to_sender_and_preserves_correlation(self):
        original = self.send(correlation_id="objective-9")
        response = self.box.reply(
            sender_id="analyst_01", in_reply_to=original["message_id"],
            kind="RESULT", payload={"ok": False, "observation": "new evidence"},
            message_id="result-1",
        )
        self.assertEqual(response["recipient_id"], "director")
        self.assertEqual(response["correlation_id"], "objective-9")
        self.assertEqual(response["in_reply_to"], original["message_id"])
        self.assertEqual(self.box.poll("director"), [response])
        self.assertEqual(self.box.reply(
            sender_id="analyst_01", in_reply_to=original["message_id"],
            kind="RESULT", payload={"ok": False, "observation": "new evidence"},
            message_id="result-1",
        ), response)
        with self.assertRaises(MessageConflict):
            self.box.reply(
                sender_id="analyst_01", in_reply_to=original["message_id"],
                kind="RESULT", payload={"ok": True}, message_id="result-1",
            )

    def test_reply_requires_participant_route_and_valid_parent(self):
        self.send()
        with self.assertRaises(KeyError):
            self.box.reply(
                sender_id="analyst_01", in_reply_to="missing",
                kind="RESULT", payload={},
            )
        with self.assertRaises(ValueError):
            self.box.reply(
                sender_id="other", in_reply_to="mission-001",
                kind="RESULT", payload={},
            )
        with self.assertRaises(ValueError):
            self.box.send(
                sender_id="analyst_01", recipient_id="director", kind="RESULT",
                payload={}, in_reply_to="mission-001", correlation_id="wrong",
            )
        with self.assertRaises(ValueError):
            self.box.send(
                sender_id="analyst_01", recipient_id="other", kind="RESULT",
                payload={}, in_reply_to="mission-001",
            )

    def test_restart_preserves_unacked_messages_and_ack_state(self):
        original = self.send()
        reply = self.box.reply(
            sender_id="analyst_01", in_reply_to="mission-001", kind="RESULT",
            payload={"status": "UNRESOLVED"}, message_id="result-1",
        )
        self.box.ack("analyst_01", "mission-001")
        self.box.close()
        with DirectorMailbox(self.path) as restarted:
            self.assertEqual(restarted.poll("analyst_01"), [])
            self.assertTrue(restarted.poll("analyst_01", include_acked=True)[0]["acked"])
            self.assertEqual(restarted.poll("director"), [reply])
            self.assertEqual(restarted.send(
                sender_id="director", recipient_id="analyst_01",
                kind="MISSION_ASSIGNMENT", payload=original["payload"],
                message_id="mission-001",
            )["acked"], True)

    def test_invalid_identifiers_types_and_poll_bounds(self):
        for bad in ("", " space", "../../tmp", "a" * 65, "a/b", None, True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.send(sender_id=bad)
        for bad in ("status", "BAD-KIND", "A" * 33, "", None):
            with self.subTest(kind=bad), self.assertRaises(ValueError):
                self.send(kind=bad)
        for bad in (-1, True, 2**63):
            with self.subTest(after_seq=bad), self.assertRaises(ValueError):
                self.box.poll("analyst_01", after_seq=bad)
        for bad in (0, True, 501):
            with self.subTest(limit=bad), self.assertRaises(ValueError):
                self.box.poll("analyst_01", limit=bad)
        with self.assertRaises(ValueError):
            self.box.poll("analyst_01", include_acked=1)

    def test_json_rejects_executable_opaque_or_nonfinite_payloads(self):
        bad_values = (
            lambda: "not a dict",
            lambda: {"nested": {"bad": object()}},
            lambda: {"bad": float("nan")},
            lambda: {"bad": float("inf")},
            lambda: {"bad": 2**66},
            lambda: {1: "nonstring-key"},
            lambda: {"bad": b"binary"},
            lambda: {"bad": "\ud800"},
            lambda: {"too_big": "x" * (32 * 1024)},
            lambda: {"many": list(range(201))},
        )
        for factory in bad_values:
            with self.subTest(payload=repr(factory())), self.assertRaises(ValueError):
                self.send(payload=factory())
        self.assertEqual(self.box.poll("analyst_01"), [])

    def test_recursive_payload_rejected_without_recursion_overflow(self):
        recursive = {}
        recursive["self"] = recursive
        with self.assertRaises(ValueError):
            self.send(payload=recursive)
        deeply_nested = {}
        for _ in range(20):
            deeply_nested = {"next": deeply_nested}
        with self.assertRaises(ValueError):
            self.send(payload=deeply_nested)

    def test_sender_is_claimed_not_authenticated_and_no_effect_grant(self):
        forged = self.send(sender_id="director")
        self.assertIs(forged["sender_authenticated"], False)
        self.assertEqual(forged["effect_authority"], "NOT_GRANTED")
        self.assertEqual(self.box.poll("analyst_01")[0], forged)

    def test_separate_connections_conflicting_id_under_contention(self):
        barrier = threading.Barrier(2)
        saved = []
        failures = []

        def send_concurrently(value):
            try:
                with DirectorMailbox(self.path) as parallel:
                    barrier.wait(timeout=3)
                    saved.append(parallel.send(
                        sender_id="director", recipient_id="analyst_01",
                        kind="STATUS", payload={"value": value},
                        message_id="same-id",
                    ))
            except Exception as exc:
                failures.append(exc)

        threads = [threading.Thread(target=send_concurrently, args=(item,))
                   for item in ("alpha", "beta")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(len(saved), 1)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], MessageConflict)
        self.assertEqual(self.box.poll("analyst_01"), saved)

    def test_close_is_effective_and_reopen_via_context_manager(self):
        self.box.close()
        with self.assertRaises(RuntimeError):
            self.box.poll("analyst_01")
        with DirectorMailbox(self.path) as reopened:
            self.assertEqual(reopened.poll("analyst_01"), [])
        with self.assertRaises(RuntimeError):
            reopened.poll("analyst_01")

    @unittest.skipUnless(os.name == "posix", "POSIX file mode check")
    def test_new_database_is_owner_private(self):
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_symlink_rejected(self):
        symlink = Path(self.tmp.name) / "link.sqlite3"
        try:
            symlink.symlink_to(self.path)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaises(ValueError):
            DirectorMailbox(symlink)


if __name__ == "__main__":
    unittest.main()
