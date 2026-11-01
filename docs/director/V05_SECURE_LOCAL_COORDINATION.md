# The Director v0.5 — Secure-local progression

This is a reproducible **same-host, data-only** research milestone. It requires
only Python 3.12+ and the standard library. Nothing here installs a service,
contacts an unknown intelligence, opens a network port, authorizes effects,
enrolls a billing account, or costs money.

## What's new

| Area | Implemented v0.5 behavior | Deliberate limitation |
| --- | --- | --- |
| Messages | Opt-in signed assignments and cancellations, verified by a per-worker inbox adapter | HMAC proves shared route-key possession, not exclusive Director authorship |
| Report durability | The coordinator commits signed-report replay uniqueness and report/state together in **one SQLite transaction** | Legacy v0.4 incoming-only mode still uses an external replay DB; there is no cross-host guarantee |
| Recovery | Explicit full-fidelity copy of a *paused* coordinator to a new, permanently quarantined DB | No mailbox/key transfer, automatic service activation, or unlock operation |
| Reasoning | Deterministic observations that could distinguish rival hypotheses, accessible in the terminal conversation | No inferred probabilities, automatic sensing, or general pretrained language model |
| Privacy | Existing coordinator/mailbox SQLite main, WAL and SHM files are checked for owner-only POSIX permissions | Parent directory and same-UID process custody remain the operator's responsibility |

## 1. Signed local coordination

To use the new protocol, construct a **fresh** `DirectorCoordinator` with
`auth_channel=...` and `sign_outbound=True`. Its authentication mode becomes
immutable `LOCAL_HMAC_BIDIRECTIONAL`. Existing `LOCAL_HMAC` databases cannot
be silently upgraded or reopened in the new mode; create a fresh coordinator
file instead. This avoids retroactively labeling old messages as having
stronger guarantees.

The operator configures **distinct** 32–128-byte keys for:

- Each worker's reports, such as `worker_alpha`.
- Each Director-to-worker route, such as `director.worker_alpha`.

Do not save actual keys in the repository, log, chat history, or plaintext
mission payload. Provision them in a private host-controlled store. For a
minimal local test, the following values stand for separately generated keys:

```python
import secrets

worker_report_key = secrets.token_bytes(32)
director_to_worker_key = secrets.token_bytes(32)
keys = {"worker_alpha": worker_report_key,
        "director.worker_alpha": director_to_worker_key}
```

These ephemeral values work for a disposable demo only. Real restartable
sessions require stable operator-managed key custody; generating fresh keys
on restart would invalidate previous pending envelopes.

An end-to-end data-only local cycle looks like this:

```python
import tempfile
import time
from pathlib import Path
from runtime.director.auth_channel import AuthenticatedChannel
from runtime.director.coordinator import DirectorCoordinator
from runtime.director.messenger import DirectorMailbox
from runtime.director.secure_worker import VerifiedWorkerInbox

with tempfile.TemporaryDirectory() as tmp:
    directory = Path(tmp)
    now = int(time.time())
    with DirectorMailbox(directory / "messages.sqlite3") as mailbox:
        with AuthenticatedChannel(directory / "director-auth.sqlite3", keys) as director_auth:
            with AuthenticatedChannel(directory / "worker-auth.sqlite3", keys) as worker_auth:
                with DirectorCoordinator(directory / "coordinator.sqlite3", mailbox,
                                         auth_channel=director_auth,
                                         sign_outbound=True) as director:
                    director.configure(
                        {"goal_id": "local.goal", "objective": "Review declared quality",
                         "metric": "quality", "target_value": 8, "direction": "INCREASE"},
                        [{"capability_id": "observe", "agent_id": "worker_alpha",
                          "metric": "quality", "expected_delta": 1, "cost_units": 0}],
                    )
                    director.tick(auth_now_utc=now)
                    worker = VerifiedWorkerInbox(mailbox, worker_auth, "worker_alpha")
                    assignment = worker.pending()[0]
                    verified = worker.verify_message(assignment, now_utc=now)
                    assert verified["worker_action_status"] == "NOT_EXECUTED"

                    reply = worker_auth.seal(
                        sender_id="worker_alpha", recipient_id="director",
                        kind="MISSION_REPORT",
                        payload={"in_reply_to": assignment["message_id"],
                                 "body": {"finding": "requires independent review"}},
                        issued_at=now, expires_at=now + 300)
                    mailbox.send(sender_id="untrusted_transport_label",
                                 recipient_id="director", kind="SIGNED_ENVELOPE",
                                 payload={"envelope": reply.decode("ascii")})
                    director.tick(auth_now_utc=now)
                    assert director.inspect()["reports"][0]["review_status"] == \
                        "MATCHED_AUTHENTICATED_UNVERIFIED"
```

`VerifiedWorkerInbox` deliberately does **not** acknowledge or execute
commands. An HMAC route secret is **symmetric**: a worker possessing its own
`director.worker_alpha` verification key could forge valid envelopes **on
that same route**. Separating route keys prevents it from trivially forging
commands for another worker, but does not establish exclusive Director origin.
For independent, potentially hostile worker processes, the next step is a
trusted principal-isolating local broker or genuinely asymmetric signatures.

Likewise, any process with direct SQLite mailbox access can claim a recipient
ID and acknowledge another recipient's message. Content authentication does
not prevent message suppression or prove availability. **Do not distribute the
shared mailbox handle or its writable database path to untrusted workers.**

## 2. Transactional report admission and retry limits

In `LOCAL_HMAC_BIDIRECTIONAL`, the coordinator uses the new non-consuming
`AuthenticatedChannel.inspect_signature(...)` method, then writes the signed
ID/nonce admission and associated report/assignment state **in the same
coordinator SQLite transaction**. Only after that commit does it acknowledge
the mailbox wrapper.

- Crash **before** coordinator commit: the unacknowledged envelope remains
  valid for retry while within its signed time window.
- Crash **after** commit but before mailbox ACK: redelivery is recognized by
  the durable report ledger and will not apply the report twice.
- Same signed ID/nonce under a different mailbox wrapper: rejected as replay.
- Crash after signed outbound mailbox send but before assignment-state commit:
  the already persisted **exact envelope bytes** are retried, preventing an
  immutable message-ID conflict.
- Signed queued intent past its expiry: dispatch fails closed. It is not
  automatically re-signed with the same message ID.

This fixes the v0.4 **coordinator-ingress** two-database crash gap in the new
mode. It is **not** exactly-once end-to-end delivery, because mailbox and
coordinator are different files, worker-side verification consumes its own
replay marker before worker application processing, and an ACK does not prove
execution or cessation. SQLite WAL is single-host and cannot make changes
to multiple attached databases crash-atomic as one set.

Reference: [SQLite ATTACH atomicity rules](https://www.sqlite.org/lang_attach.html),
[SQLite WAL](https://www.sqlite.org/wal.html), and
[HMAC RFC 2104](https://www.rfc-editor.org/rfc/rfc2104.html).

## 3. Full-fidelity recovery without live resumption

The existing `RecoveryStore` archives **diagnostic** `inspect()` snapshots.
Those are useful audit records but omit internal data needed to rebuild a
running coordinator. Do **not** interpret them as a complete runtime image.

The new `stage_recovery_copy` instead uses the standard SQLite backup API to
copy the **entire coordinator database**, with a complete record of goals,
capabilities, graphs, assignments, attestation flags, and signed intent
metadata, into a NEW destination. The source must first be explicitly
paused. Its separate mailbox and authentication ledgers are **not** copied.

```python
from pathlib import Path
from runtime.director.recovery_copy import stage_recovery_copy

# Pause the actual source coordinator before this step:
# director.set_paused(True)
destination = Path("./director-quarantined.sqlite3").resolve()
receipt = stage_recovery_copy(
    Path("./director-coordinator.sqlite3").resolve(), destination,
    operator_approval={
        "approved": True,
        "purpose": "PAUSED_QUARANTINE_DATA_ONLY",
        "source_goal_id": "local.goal",
        "destination_path": str(destination),
        "allow_historical_inflight": False,
    },
)
assert receipt["quarantined"] is True
```

The destination must not already exist. Its durable `director_recovery_quarantine`
lock prevents `tick`, cancellation delivery, outbound dispatch, and state
mutation even when its coordinator is reopened with matching local auth
configuration. Old in-flight assignment records are inert historical data;
there is intentionally **no unlock or automatic reactivation API**. Manual
source/mailbox/key reconciliation and independent authority checks are
required before a future reactivation design is considered.

This method makes one consistent SQLite database copy; it does not form a
single atomic recovery unit with the old mailbox, HMAC ledger, worker
processes, or a remote host. Protect backup directories from untrusted local
writers and keep independent copies for genuine disk-loss recovery.

Reference: [Python sqlite3 backup](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup)
and [SQLite Online Backup API](https://sqlite.org/backup.html).

## 4. Local investigations that can challenge hypotheses

`LocalReasoner.suggest_discriminating_observations(world_snapshot,hypotheses)`
proposes which missing or contested measurements would separate the most
still-viable rival hypotheses under their declared predictions. The count is
**not a probability or truth score**. A prediction is data, not an observed
fact or authority. Contradicted candidates are tracked, not silently
converted into accepted explanations.

For the terminal CLI, put a bounded JSON array in a local file:

```json
[
  {"hypothesis_id":"h1","predictions":{"quality":3,"switch_on":true}},
  {"hypothesis_id":"h2","predictions":{"quality":6,"switch_on":false}}
]
```

Then start/resume `python3 -m runtime.director.chat_cli ...
--hypotheses-json ./hypotheses.json` and ask *“What observation would best
distinguish the hypotheses?”* For a crash-pending conversation, choose
`--resume-policy PINNED_CONTEXT` to require exact context replay, or
`--resume-policy FRESH_REAPPRAISAL` (the backward-compatible default) to
explicitly label an answer computed under updated evidence. Context
digests and revision changes are recorded; no hidden change of the
operator's goal is allowed through conversation text.

These ideas are informed by the typed falsifier/verification contracts of
Patrick's other repositories, especially Rezon and the existing cognitive
core, without importing credentials, personal memories or host access.

## Qualification and remaining work

```bash
python3 -B -m unittest discover -s tests -q
python3 tools/validate_hc_architecture.py --root .
python3 -B -m examples.director_local_cycle
```

Current tests address signed route binding, spoofed unsigned commands,
tampering, expiry, replay, crash-injected report rollback, restart-after-ACK
failure, persistent signed-envelope retries, quarantine restart without
dispatch, blocked cancellation/stop methods, database permissions, and
hypothesis probe selection.

Before multi-device operation, establish:

1. A trusted per-principal broker or asymmetric Director signing keys,
   plus protected local key custody and true inbox/ACK access control.
2. A crash-recoverable worker receipt/processing protocol, separate from
   the coordinator's newly atomic ingress.
3. OS-enforced process resource isolation and independent host identity.
4. Explicit supervised reactivation after reconciliation of coordinator,
   mailbox, credentials and active worker state.

The absolute incremental cash budget is **$0**. No Vercel, paid inference,
billing enrollment, cloud service or surprise network effects are included.
