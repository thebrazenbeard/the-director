"""Verified *local* worker-side reception of Director analytical messages.

This module does not run assigned work, acknowledge messages, or open ports.
The worker explicitly polls its local mailbox and verifies an individual
signed envelope before considering its contents. The outer SQLite mailbox
``sender_id`` is **never trusted**; the signed route sender is
``director.<worker_id>`` and the inner identity must match the mailbox ID.

Each worker needs its own route secret. HMAC is symmetric: possession of a
route key lets its holder forge messages *on that route*, so this is NOT
cryptographic proof of exclusive Director authorship or human authority.
No route-specific HMAC receipt confers protected-effect permission.

``verify_message`` consumes the worker's replay marker via
``AuthenticatedChannel.verify``. That marker and mailbox acknowledgment are
not committed atomically: a crash after verification can make a later retry
appear replayed. There are no automatic acknowledgments or effects; callers
must supply their own durable processing semantics before real workloads.
"""

from __future__ import annotations

from collections.abc import Mapping

from .auth_channel import AuthenticatedChannel, _identifier
from .messenger import DirectorMailbox


_KIND_ID = {
    "MISSION_ASSIGNMENT": "assignment_message_id",
    "MISSION_CANCEL": "cancellation_message_id",
}
_MAX_WRAPPER = 16384


class VerifiedWorkerInbox:
    """Local worker mailbox adapter; *verification only*, never execution.

    A trusted host must isolate each worker's mailbox and route key. Anyone
    able to mutate or ACK the shared SQLite mailbox can still suppress traffic;
    authenticated message content is not a mailbox availability guarantee.
    """

    def __init__(
        self, mailbox: DirectorMailbox, channel: AuthenticatedChannel,
        worker_id: str,
    ) -> None:
        if not isinstance(mailbox, DirectorMailbox):
            raise TypeError("mailbox must be a DirectorMailbox")
        if not isinstance(channel, AuthenticatedChannel):
            raise TypeError("channel must be an AuthenticatedChannel")
        self.worker_id = _identifier(worker_id, "worker_id")
        self.director_route_id = _identifier("director." + self.worker_id, "director route ID")
        self.mailbox = mailbox
        self.channel = channel
        if not self.channel.has_sender_key(self.director_route_id):
            raise ValueError("worker requires its own configured active Director route key")

    def pending(self, *, limit: int = 100) -> list[dict[str, object]]:
        """Return untrusted mailbox rows for explicit verification (no ACK)."""
        return self.mailbox.poll(self.worker_id, limit=limit)

    def verify_message(
        self, message: Mapping[str, object], *, now_utc: int,
    ) -> dict[str, object]:
        """Validate and consume one Director-route message, without acting.

        All application-schema checks run against the non-consuming signature
        inspector first, so malformed but correctly signed commands cannot
        accidentally consume a replay marker through this adapter. The final
        ``verify`` call rechecks the MAC/revocation and consumes one token.
        """
        if not isinstance(message, Mapping):
            raise ValueError("worker message must be a mailbox mapping")
        outer_id = _identifier(message.get("message_id"), "mailbox message_id")
        if message.get("recipient_id") != self.worker_id:
            raise ValueError("mailbox recipient differs from the worker")
        if message.get("kind") != "SIGNED_ENVELOPE":
            raise ValueError("worker will not accept unsigned Director commands")
        wrapper = message.get("payload")
        if not isinstance(wrapper, Mapping) or set(wrapper) != {"envelope"}:
            raise ValueError("worker signed wrapper has invalid fields")
        envelope = wrapper["envelope"]
        if type(envelope) is not str or len(envelope) > _MAX_WRAPPER:
            raise ValueError("worker signed envelope must be bounded JSON text")
        try:
            raw = envelope.encode("ascii")
        except UnicodeError as exc:
            raise ValueError("worker signed envelope must be canonical ASCII JSON") from exc

        checked = self.channel.inspect_signature(
            raw, expected_recipient=self.worker_id, now_utc=now_utc)
        if checked["authenticated_sender_id"] != self.director_route_id:
            raise ValueError("signed Director route does not match this worker")
        if checked["message_id"] != outer_id:
            raise ValueError("signed envelope ID does not match mailbox message ID")
        kind = checked["kind"]
        if kind not in _KIND_ID:
            raise ValueError("signed command kind is not admitted for worker reception")
        payload = checked["payload"]
        identity_key = _KIND_ID[kind]
        if not isinstance(payload, Mapping) or set(payload) != {identity_key, "body"}:
            raise ValueError("signed command must contain exact identity and body fields")
        if _identifier(payload[identity_key], identity_key) != outer_id:
            raise ValueError("inner signed command identity differs from mailbox message")
        body = payload["body"]
        if not isinstance(body, dict):
            raise ValueError("signed command body must be a JSON object")
        if (kind == "MISSION_ASSIGNMENT" and body.get("assigned_agent_id") != self.worker_id):
            raise ValueError("signed assignment is addressed to a different worker")
        if kind == "MISSION_CANCEL":
            _identifier(body.get("assignment_message_id"), "canceled assignment ID")
        if body.get("effect_authority", "NOT_GRANTED") != "NOT_GRANTED":
            raise ValueError("signed command cannot include an effect grant")
        if body.get("authorization", "NOT_GRANTED") != "NOT_GRANTED":
            raise ValueError("signed command cannot claim host authorization")
        if body.get("effect_status", "NOT_EXECUTED") != "NOT_EXECUTED":
            raise ValueError("signed command cannot assert a completed effect")
        if body.get("worker_stopped_verified", False) is not False:
            raise ValueError("signed command cannot assert verified worker cessation")

        admitted = self.channel.verify(raw, expected_recipient=self.worker_id,
                                       now_utc=now_utc)
        return {
            "message_id": outer_id,
            "worker_id": self.worker_id,
            "kind": kind,
            "body": dict(body),
            "authenticated_sender_id": admitted["authenticated_sender_id"],
            "sender_authenticated": True,
            "authentication_scope": "WORKER_SCOPED_SHARED_HMAC_KEY",
            "replay_admitted": True,
            "effect_authority": "NOT_GRANTED",
            "effect_status": "NOT_EXECUTED",
            "worker_action_status": "NOT_EXECUTED",
            "mailbox_acked": False,
        }


__all__ = ["VerifiedWorkerInbox"]
