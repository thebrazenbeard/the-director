"""Worker-side HMAC route verification of advisory Director mailbox commands."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from runtime.director.auth_channel import AuthenticatedChannel
from runtime.director.messenger import DirectorMailbox
from runtime.director.secure_worker import VerifiedWorkerInbox


NOW = 1_800_000_000
KEYS = {"director.worker_alpha": b"A" * 32,
        "director.worker_beta": b"B" * 32}


class VerifiedWorkerInboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        parent = Path(self.temp.name)
        self.mailbox = DirectorMailbox(parent / "mail.sqlite3")
        self.addCleanup(self.mailbox.close)
        self.channel = AuthenticatedChannel(parent / "auth.sqlite3", KEYS)
        self.addCleanup(self.channel.close)
        self.inbox = VerifiedWorkerInbox(self.mailbox, self.channel, "worker_alpha")

    def _signed(self, *, ident="mission.1", worker="worker_alpha",
                signer=None, kind="MISSION_ASSIGNMENT", body=None,
                inner_id=None, issued=NOW, expires=NOW + 300):
        signer = signer or "director." + worker
        if body is None:
            body = ({"assigned_agent_id": worker,
                     "effect_authority": "NOT_GRANTED", "effect_status": "NOT_EXECUTED"}
                    if kind == "MISSION_ASSIGNMENT"
                    else {"assignment_message_id": "mission.1", "effect_status": "NOT_EXECUTED"})
        identity_field = ("assignment_message_id" if kind == "MISSION_ASSIGNMENT"
                          else "cancellation_message_id")
        return self.channel.seal(
            sender_id=signer, recipient_id=worker, kind=kind,
            payload={identity_field: ident if inner_id is None else inner_id, "body": body},
            issued_at=issued, expires_at=expires, message_id=ident,
        )

    def _mail(self, raw, *, ident="mission.1", recipient="worker_alpha",
              kind="SIGNED_ENVELOPE", sender="untrusted.claimed.sender"):
        return self.mailbox.send(sender_id=sender, recipient_id=recipient,
                                 kind=kind, payload={"envelope": raw.decode("ascii")},
                                 message_id=ident)

    def test_assignment_verified_without_trusting_outer_sender_or_acking(self):
        self._mail(self._signed(), sender="intruder")
        raw_messages = self.inbox.pending()
        self.assertEqual(len(raw_messages), 1)
        self.assertFalse(raw_messages[0]["sender_authenticated"])
        accepted = self.inbox.verify_message(raw_messages[0], now_utc=NOW + 1)
        self.assertTrue(accepted["sender_authenticated"])
        self.assertEqual(accepted["authenticated_sender_id"], "director.worker_alpha")
        self.assertEqual(accepted["kind"], "MISSION_ASSIGNMENT")
        self.assertEqual(accepted["body"]["assigned_agent_id"], "worker_alpha")
        self.assertTrue(accepted["replay_admitted"])
        self.assertFalse(accepted["mailbox_acked"])
        self.assertEqual(accepted["effect_authority"], "NOT_GRANTED")
        self.assertEqual(accepted["worker_action_status"], "NOT_EXECUTED")
        self.assertEqual(len(self.inbox.pending()), 1)
        with self.assertRaisesRegex(ValueError, "replay"):
            self.inbox.verify_message(raw_messages[0], now_utc=NOW + 1)
        self.mailbox.ack("worker_alpha", "mission.1")
        self.assertEqual(self.inbox.pending(), [])

    def test_cancel_notice_uses_same_route_and_never_proves_worker_stopped(self):
        notice = self._mail(self._signed(ident="cancel.1", kind="MISSION_CANCEL"), ident="cancel.1")
        result = self.inbox.verify_message(notice, now_utc=NOW + 2)
        self.assertEqual(result["kind"], "MISSION_CANCEL")
        self.assertEqual(result["body"]["assignment_message_id"], "mission.1")
        self.assertEqual(result["effect_status"], "NOT_EXECUTED")
        self.assertEqual(result["worker_action_status"], "NOT_EXECUTED")

    def test_unsigned_forgery_and_wrong_worker_route_are_rejected(self):
        raw = self._signed()
        forged = self._mail(raw, kind="MISSION_ASSIGNMENT")
        with self.assertRaisesRegex(ValueError, "unsigned"):
            self.inbox.verify_message(forged, now_utc=NOW + 1)
        self.assertFalse(forged["sender_authenticated"])

        # A separately keyed worker's Director route cannot speak for alpha,
        # even when the envelope recipient is signed as alpha.
        wrong_route = self.channel.seal(
            sender_id="director.worker_beta", recipient_id="worker_alpha",
            kind="MISSION_ASSIGNMENT",
            payload={"assignment_message_id": "mission.2",
                     "body": {"assigned_agent_id": "worker_alpha"}},
            issued_at=NOW, expires_at=NOW + 300, message_id="mission.2",
        )
        wrapped = self._mail(wrong_route, ident="mission.2")
        with self.assertRaisesRegex(ValueError, "route"):
            self.inbox.verify_message(wrapped, now_utc=NOW + 1)

    def test_tamper_wrong_recipient_expiry_and_signature_errors_fail_closed(self):
        raw = self._signed()
        encoded = json.loads(raw)
        encoded["payload"]["body"]["assigned_agent_id"] = "intruder"
        for bad_raw, now in ((json.dumps(encoded).encode("ascii"), NOW + 1),
                             (raw, NOW + 300), (raw, NOW - 1)):
            msg = {"message_id": "mission.1", "recipient_id": "worker_alpha",
                   "kind": "SIGNED_ENVELOPE", "payload": {"envelope": bad_raw.decode("ascii")}}
            with self.subTest(now=now), self.assertRaises(ValueError):
                self.inbox.verify_message(msg, now_utc=now)

        wrong_recipient = {"message_id": "mission.1", "recipient_id": "worker_beta",
                           "kind": "SIGNED_ENVELOPE", "payload": {"envelope": raw.decode("ascii")}}
        with self.assertRaisesRegex(ValueError, "recipient"):
            self.inbox.verify_message(wrong_recipient, now_utc=NOW + 1)
        with self.assertRaises(ValueError):
            self.inbox.verify_message({**wrong_recipient, "recipient_id": "worker_alpha",
                                       "payload": {"envelope": "not-json"}}, now_utc=NOW + 1)
        # Invalid attempts didn't consume the valid nonce.
        self._mail(raw)
        self.assertTrue(self.inbox.verify_message(self.inbox.pending()[0],
                                                  now_utc=NOW + 1)["replay_admitted"])

    def test_signed_identity_and_shape_binding_cannot_consume_good_message(self):
        raw = self._signed()
        invalid = {"message_id": "different.id", "recipient_id": "worker_alpha",
                   "kind": "SIGNED_ENVELOPE", "payload": {"envelope": raw.decode("ascii")}}
        with self.assertRaisesRegex(ValueError, "ID"):
            self.inbox.verify_message(invalid, now_utc=NOW + 1)
        wrong_inner = self._signed(ident="mission.3", inner_id="mission.other")
        with self.assertRaisesRegex(ValueError, "identity"):
            self.inbox.verify_message({**invalid, "message_id": "mission.3",
                                       "payload": {"envelope": wrong_inner.decode("ascii")}},
                                      now_utc=NOW + 1)

        wrong_body = self._signed(ident="mission.4", body={"assigned_agent_id": "worker_beta"})
        with self.assertRaisesRegex(ValueError, "different worker"):
            self.inbox.verify_message({**invalid, "message_id": "mission.4",
                                       "payload": {"envelope": wrong_body.decode("ascii")}},
                                      now_utc=NOW + 1)
        self._mail(raw)
        self.assertTrue(self.inbox.verify_message(self.inbox.pending()[0],
                                                  now_utc=NOW + 1)["sender_authenticated"])

    def test_invalid_effect_claim_bad_cancel_and_unknown_kind_rejected(self):
        grant = self._signed(ident="grant.1", body={"assigned_agent_id": "worker_alpha",
                                                     "effect_authority": "GRANTED"})
        msg = {"message_id": "grant.1", "recipient_id": "worker_alpha",
               "kind": "SIGNED_ENVELOPE", "payload": {"envelope": grant.decode("ascii")}}
        with self.assertRaisesRegex(ValueError, "effect grant"):
            self.inbox.verify_message(msg, now_utc=NOW + 1)
        claim = self._signed(ident="grant.2", body={"assigned_agent_id": "worker_alpha",
                                                     "authorization": "APPROVED"})
        with self.assertRaisesRegex(ValueError, "host authorization"):
            self.inbox.verify_message({**msg, "message_id": "grant.2",
                                       "payload": {"envelope": claim.decode("ascii")}},
                                      now_utc=NOW + 1)
        stopped = self._signed(ident="cancel.3", kind="MISSION_CANCEL",
                               body={"assignment_message_id": "mission.1",
                                     "worker_stopped_verified": True})
        with self.assertRaisesRegex(ValueError, "cessation"):
            self.inbox.verify_message({**msg, "message_id": "cancel.3",
                                       "payload": {"envelope": stopped.decode("ascii")}},
                                      now_utc=NOW + 1)
        bad_cancel = self._signed(ident="cancel.2", kind="MISSION_CANCEL", body={})
        with self.assertRaises(ValueError):
            self.inbox.verify_message({**msg, "message_id": "cancel.2",
                                       "payload": {"envelope": bad_cancel.decode("ascii")}},
                                      now_utc=NOW + 1)
        unsupported = self.channel.seal(sender_id="director.worker_alpha", recipient_id="worker_alpha",
                                        kind="MISSION_REPORT", payload={}, issued_at=NOW,
                                        expires_at=NOW + 300, message_id="report.1")
        with self.assertRaisesRegex(ValueError, "kind"):
            self.inbox.verify_message({**msg, "message_id": "report.1",
                                       "payload": {"envelope": unsupported.decode("ascii")}},
                                      now_utc=NOW + 1)

    def test_distinct_route_required_and_long_id_rejected(self):
        parent = Path(self.temp.name)
        with AuthenticatedChannel(parent / "wrongkeys.sqlite3", {"director": b"Z" * 32}) as other:
            with self.assertRaisesRegex(ValueError, "route key"):
                VerifiedWorkerInbox(self.mailbox, other, "worker_alpha")
        with self.assertRaisesRegex(ValueError, "bounded identifier"):
            VerifiedWorkerInbox(self.mailbox, self.channel, "x" * 63)


if __name__ == "__main__":
    unittest.main()
