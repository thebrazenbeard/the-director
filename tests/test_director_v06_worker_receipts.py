"""Durable, data-only worker signed-command receipts; no mailbox ACK/effects."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from runtime.director.auth_channel import AuthenticatedChannel
from runtime.director.messenger import DirectorMailbox
from runtime.director.secure_worker import VerifiedWorkerInbox
from runtime.director.worker_receipts import DurableWorkerReceipts


KEY = b"W" * 32
NOW = 200


class DurableWorkerReceiptsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.mailbox = DirectorMailbox(root / "mailbox.db")
        self.addCleanup(self.mailbox.close)
        self.channel = AuthenticatedChannel(root / "auth.db", {"director.worker_x": KEY})
        self.addCleanup(self.channel.close)
        self.inbox = VerifiedWorkerInbox(self.mailbox, self.channel, "worker_x")
        self.path = root / "receipts.db"
        self.receipts = DurableWorkerReceipts(self.path, self.inbox)
        self.addCleanup(lambda: self.receipts.close())

    def signed(self, *, message_id="assignment.1", nonce="a" * 32,
               kind="MISSION_ASSIGNMENT", worker="worker_x", body=None):
        if body is None:
            body = {"assigned_agent_id": worker, "goal_id": "quality"}
        key = "assignment_message_id" if kind == "MISSION_ASSIGNMENT" else "cancellation_message_id"
        raw = self.channel.seal(sender_id="director.worker_x", recipient_id=worker,
                                kind=kind, payload={key: message_id, "body": body},
                                issued_at=100, expires_at=300, message_id=message_id,
                                nonce=nonce)
        return self.mailbox.send(sender_id="untrusted_outer", recipient_id=worker,
                                 kind="SIGNED_ENVELOPE", payload={"envelope": raw.decode("ascii")},
                                 message_id=message_id)

    def test_receipt_survives_restart_and_never_acks_or_executes(self):
        command = self.signed()
        before = self.mailbox.poll("worker_x")
        inspected = self.inbox.inspect_message(command, now_utc=NOW)
        self.assertFalse(inspected["replay_admitted"])
        self.assertEqual(inspected["effect_status"], "NOT_EXECUTED")
        saved = self.receipts.accept(command, now_utc=NOW)
        self.assertEqual(saved["state"], "PENDING_REVIEW")
        self.assertFalse(saved["mailbox_acked"])
        self.assertEqual(saved["effect_status"], "NOT_EXECUTED")
        self.assertEqual(saved["signed_message_id"], "assignment.1")
        self.assertEqual(self.mailbox.poll("worker_x"), before)
        # Auth channel's distinct replay ledger was not consumed by inspect.
        with closing(sqlite3.connect(self.channel.path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM authenticated_replay").fetchone()[0], 0)
        self.receipts.close()
        self.receipts = DurableWorkerReceipts(self.path, self.inbox)
        replay = self.receipts.accept(command, now_utc=NOW + 1)
        self.assertEqual(replay, saved)
        self.assertEqual(self.receipts.receipt("assignment.1"), saved)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM director_worker_receipts").fetchone()[0], 1)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(director_worker_receipts)")}
            self.assertNotIn("payload_json", columns)
            self.assertNotIn("envelope", columns)

    def test_replay_message_id_or_nonce_reused_with_other_payload_is_rejected(self):
        first = self.signed()
        self.receipts.accept(first, now_utc=NOW)
        second_raw = self.channel.seal(sender_id="director.worker_x", recipient_id="worker_x",
                                       kind="MISSION_ASSIGNMENT",
                                       payload={"assignment_message_id": "assignment.1",
                                                "body": {"assigned_agent_id": "worker_x", "goal_id": "different"}},
                                       issued_at=100, expires_at=300,
                                       message_id="assignment.1", nonce="b" * 32)
        conflict = {**first, "payload": {"envelope": second_raw.decode("ascii")}}
        with self.assertRaisesRegex(ValueError, "replay|conflict|different"):
            self.receipts.accept(conflict, now_utc=NOW)
        second = self.signed(message_id="assignment.2", nonce="a" * 32)
        with self.assertRaisesRegex(ValueError, "replay|conflict|nonce"):
            self.receipts.accept(second, now_utc=NOW)
        self.assertEqual(self.receipts.receipt("assignment.1")["envelope_sha256"],
                         hashlib.sha256(first["payload"]["envelope"].encode("ascii")).hexdigest())
        self.assertIsNone(self.receipts.receipt("assignment.2"))

    def test_semantically_identical_signed_json_whitespace_is_idempotent(self):
        command = self.signed()
        original = self.receipts.accept(command, now_utc=NOW)
        pretty_json = json.dumps(json.loads(command["payload"]["envelope"]), indent=2)
        variant = {**command, "payload": {"envelope": pretty_json}}
        self.assertEqual(self.receipts.accept(variant, now_utc=NOW), original)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM director_worker_receipts").fetchone()[0], 1)

    def test_no_mailbox_ack_even_for_other_recipients(self):
        command = self.signed()
        self.mailbox.send(sender_id="worker_z", recipient_id="director", kind="PING",
                          payload={"declared": True}, message_id="inbound.1")
        self.mailbox.send(sender_id="director", recipient_id="worker_y", kind="PING",
                          payload={"declared": True}, message_id="outbound.1")
        recipients = ("worker_x", "worker_y", "director")
        before = {recipient: self.mailbox.poll(recipient) for recipient in recipients}
        with patch.object(self.mailbox, "ack", side_effect=AssertionError("ACK not authorized")):
            receipt = self.receipts.accept(command, now_utc=NOW)
        self.assertEqual(receipt["state"], "PENDING_REVIEW")
        self.assertEqual({recipient: self.mailbox.poll(recipient) for recipient in recipients}, before)

    def test_receipt_capacity_bound_fails_closed_without_ack_or_insert(self):
        # A DB fixture efficiently exercises the capacity branch without
        # forging thousands of command signatures in the test itself.
        rows = [(f"seed.{i}", f"{i:032x}", "worker_x", "director.worker_x",
                 "MISSION_ASSIGNMENT", 300, "0" * 64) for i in range(2048)]
        with self.receipts._transaction() as conn:
            conn.executemany("""INSERT INTO director_worker_receipts (
                signed_message_id,nonce,worker_id,signed_route_id,command_kind,
                expires_at,envelope_sha256,state) VALUES (?,?,?,?,?,?,?,'PENDING_REVIEW')""", rows)
        command = self.signed()
        with self.assertRaisesRegex(ValueError, "capacity exceeded"):
            self.receipts.accept(command, now_utc=NOW)
        self.assertIsNone(self.receipts.receipt("assignment.1"))
        self.assertEqual(len(self.mailbox.poll("worker_x")), 1)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM director_worker_receipts").fetchone()[0], 2048)

    def test_invalid_route_signature_and_unsigned_command_never_persist(self):
        command = self.signed()
        variants = [
            {**command, "recipient_id": "another_worker"},
            {**command, "kind": "MISSION_ASSIGNMENT"},
            {**command, "payload": {"envelope": command["payload"]["envelope"].replace("quality", "fraud")}},
            {**command, "message_id": "wrong.id"},
        ]
        for invalid in variants:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.receipts.accept(invalid, now_utc=NOW)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM director_worker_receipts").fetchone()[0], 0)
        self.channel.revoke_sender("director.worker_x")
        with self.assertRaisesRegex(ValueError, "revoked"):
            self.receipts.accept(command, now_utc=NOW)

    def test_reject_expired_fresh_command_and_wrong_worker_binding(self):
        command = self.signed()
        with self.assertRaisesRegex(ValueError, "expired"):
            self.receipts.accept(command, now_utc=301)
        self.receipts.close()
        other = VerifiedWorkerInbox(self.mailbox, self.channel, "worker_x")
        with DurableWorkerReceipts(self.path, other) as recovered:
            self.assertIsNone(recovered.receipt("assignment.1"))
        # A valid receipt ledger is bound to the original configured worker.
        second_channel_path = self.path.with_name("other-auth.db")
        with AuthenticatedChannel(second_channel_path, {"director.worker_y": b"Y" * 32}) as channel:
            another_inbox = VerifiedWorkerInbox(self.mailbox, channel, "worker_y")
            with self.assertRaisesRegex(ValueError, "worker|bound"):
                DurableWorkerReceipts(self.path, another_inbox)

    def test_receipts_must_use_a_separate_database_from_mailbox_and_keys(self):
        for prohibited in (self.mailbox.path, self.channel.path):
            with self.subTest(path=prohibited):
                with self.assertRaisesRegex(ValueError, "separate"):
                    DurableWorkerReceipts(prohibited, self.inbox)

    @unittest.skipUnless(os.name == "posix", "POSIX hardlink qualification")
    def test_existing_hardlink_aliases_are_rejected_before_opening_sqlite(self):
        root = Path(self.temp.name)
        for source in (self.mailbox.path, self.channel.path):
            alias = root / (source.stem + "-hardlink.db")
            os.link(source, alias)
            try:
                with self.assertRaisesRegex(ValueError, "hardlink"):
                    DurableWorkerReceipts(alias, self.inbox)
            finally:
                alias.unlink()
        alias = root / "receipt-alias.db"
        os.link(self.path, alias)
        try:
            with self.assertRaisesRegex(ValueError, "hardlink"):
                DurableWorkerReceipts(self.path, self.inbox)
        finally:
            alias.unlink()

    @unittest.skipUnless(os.name == "posix", "POSIX active SQLite sidecar qualification")
    def test_auth_wal_shm_hardlinks_are_rejected_without_touching_auth_db(self):
        # Keep the authentication channel open so the WAL and SHM are active.
        self.assertTrue(self.channel.has_sender_key("director.worker_x"))
        self.receipts.close()
        original_auth = self.channel._connection()
        self.assertEqual(original_auth.execute("PRAGMA quick_check").fetchone()[0], "ok")
        for suffix in ("-wal", "-shm", "-journal"):
            source = (Path(str(self.channel.path) + suffix)
                      if suffix != "-journal" else self.channel.path)
            self.assertTrue(source.is_file(), f"active auth SQLite file missing: {source}")
            alias = Path(str(self.path) + suffix)
            self.assertFalse(alias.exists())
            os.link(source, alias)
            try:
                with self.subTest(sidecar=suffix):
                    with self.assertRaisesRegex(ValueError, "hardlink"):
                        DurableWorkerReceipts(self.path, self.inbox)
                    # A failed open must not have unlinked or repurposed the
                    # planted alias, and the original auth DB must remain sound.
                    self.assertTrue(alias.is_file())
                    self.assertTrue(os.path.samefile(alias, source))
                    self.assertEqual(original_auth.execute("PRAGMA quick_check").fetchone()[0], "ok")
            finally:
                alias.unlink()

    def test_injected_database_failure_rolls_back_without_consuming_signature(self):
        command = self.signed()
        with self.receipts._transaction() as conn:
            conn.execute("""CREATE TRIGGER injected_failure BEFORE INSERT ON director_worker_receipts
                BEGIN SELECT RAISE(ABORT,'injected receipt crash'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected receipt crash"):
            self.receipts.accept(command, now_utc=NOW)
        self.assertIsNone(self.receipts.receipt("assignment.1"))
        self.assertEqual(len(self.mailbox.poll("worker_x")), 1)
        with self.receipts._transaction() as conn:
            conn.execute("DROP TRIGGER injected_failure")
        self.assertEqual(self.receipts.accept(command, now_utc=NOW)["state"], "PENDING_REVIEW")

    def test_sqlite_trigger_auto_rollback_preserves_original_failure(self):
        """A SQLite RAISE(ROLLBACK) must not be masked by redundant cleanup."""
        command = self.signed()
        with self.receipts._transaction() as conn:
            conn.execute("""CREATE TRIGGER auto_rollback BEFORE INSERT ON director_worker_receipts
                BEGIN SELECT RAISE(ROLLBACK,'original receipt rollback'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "original receipt rollback"):
            self.receipts.accept(command, now_utc=NOW)
        self.assertFalse(self.receipts._connection().in_transaction)
        self.assertIsNone(self.receipts.receipt("assignment.1"))
        self.assertEqual(len(self.mailbox.poll("worker_x")), 1)
        with self.receipts._transaction() as conn:
            conn.execute("DROP TRIGGER auto_rollback")
        self.assertEqual(self.receipts.accept(command, now_utc=NOW)["state"], "PENDING_REVIEW")

    @unittest.skipUnless(os.name == "posix", "POSIX mode protection")
    def test_existing_public_or_symlink_database_is_rejected(self):
        self.receipts.close()
        self.path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "owner-only"):
            DurableWorkerReceipts(self.path, self.inbox)
        self.path.chmod(0o600)
        link = self.path.with_name("linked-receipts.db")
        link.symlink_to(self.path)
        with self.assertRaisesRegex(ValueError, "symlink"):
            DurableWorkerReceipts(link, self.inbox)


if __name__ == "__main__":
    unittest.main()
