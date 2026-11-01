# The Director v0.4 — Local operating contract

**Historical foundation:** This guide documents the v0.4 protocol modes.
For the newer opt-in signed-outbound / coordinator-atomic-ingress mode and
full-fidelity quarantined recovery copies, see the
[v0.5 secure-local operating guide](V05_SECURE_LOCAL_COORDINATION.md).

This release is a **working, bounded, no-cost, same-host reference**, not a
general intelligence, autonomous network daemon, or certified isolation system.
It has four separate opt-in components; they do not silently acquire one
another's permissions. No paid service, credential provider, port or cloud
deployment is required.

## 1. Dependency-aware coordination

`DirectorCoordinator.configure(goal, capabilities)` registers a goal and a
bounded catalog of **analytical** capabilities. After that, call
`configure_graph(nodes, resource_capacities)` before the first assignment.
Each `mission_id` in the graph must match a registered `capability_id`.
Dependencies, shared resources, logical deadlines and declared resource
outages constrain actual mission dispatch in `tick()`.

```python
from pathlib import Path
from runtime.director.coordinator import DirectorCoordinator
from runtime.director.messenger import DirectorMailbox

with DirectorMailbox(Path("./director-mailbox.sqlite3")) as mailbox:
    with DirectorCoordinator(Path("./director-coordinator.sqlite3"), mailbox) as director:
        director.configure(
            {"goal_id": "quality.goal", "objective": "Review a quality metric",
             "metric": "quality", "target_value": 8, "direction": "INCREASE"},
            [{"capability_id": "observe", "agent_id": "observer", "metric": "quality",
              "expected_delta": 1, "cost_units": 0},
             {"capability_id": "analyze", "agent_id": "analyst", "metric": "quality",
              "expected_delta": 1, "cost_units": 0}],
        )
        director.configure_graph(
            [{"mission_id": "observe", "kind": "OBSERVE", "depends_on": [],
              "resources": {"reviewer": 1}, "priority": 20, "cost_units": 0},
             {"mission_id": "analyze", "kind": "ANALYZE", "depends_on": ["observe"],
              "resources": {"reviewer": 1}, "priority": 10, "cost_units": 0}],
            {"reviewer": 1},
        )
        print(director.tick()["new_missions"])
        print(director.inspect()["graph"])
```

The `analyze` assignment waits for `observe` to have (a) a reported mission,
(b) a newer, noncontested `OBSERVATION` fact `completed.observe=True`, and
(c) an explicit `confirm_observation` and
`confirm_graph_node_completed` operator review. A report saying "done" is
**not** enough. Later contradictory evidence revokes graph readiness and
enqueues cooperative cancellations of downstream assignments. It does **not**
prove an already-running worker stopped. Graph configuration is immutable
once missions are issued, and `set_graph_logical_tick` is an operator-supplied,
monotonic logical clock, not authenticated time.

## 2. Local reasoning and actual conversation

`LocalReasoner` compares declared numeric baselines, conditional projections,
rival hypotheses and evidence gaps; it has no pretrained language model and
does not invent an observation. `DirectorDialogue` adds persistent multi-turn
history, crash-pending answer recovery, and explicit pause/resume. Start the
foreground terminal interface in the repository root:

```bash
python3 -m runtime.director.chat_cli --database ./director-chat.sqlite3 \
  --thread local --goal-id quality.goal --objective "Review local evidence" \
  --metric quality --target 8 --direction INCREASE --observed-value 3
```

Use `/history`, `/pause`, `/resume`, and `/quit`; reopen with the same
`--database` and `--thread` to continue. Observed values and supplied JSON
snapshots are **declared context**, not sensor measurements. Do not enter
private memories, credentials or personally identifiable data into the
plaintext conversation database by default.

For an already configured local coordinator, the CLI also offers a read-only
`--coordinator-db` argument: it reads its existing goal and replayed world
events on each question. It does not tick, dispatch, edit coordinator state,
authenticate event origins, or gain effect authority. The coordinator and
dialogue use separate files.

## 3. Explicit software-agent state import and execution

First `export_agent_state` and `restore_agent_state` the selected data via
the original consent-bound capsule interface. Only then can a **separate**
local operator decision call:

```python
from runtime.director.agent_runtime import IsolatedAgentRuntime

# restored_record is the previously obtained restore_agent_state(...) record.
# It contains permitted software-agent data, not Python code or permissions.
approval = {"approved": True, "agent_id": restored_record["agent_id"],
            "new_instance_id": restored_record["new_instance_id"],
            "target_domain": restored_record["target_domain"],
            "purpose": "DATA_ONLY_LOCAL_RUNTIME"}
with IsolatedAgentRuntime.start(restored_record, operator_approval=approval) as instance:
    print(instance.status())
    print(instance.think())
```

The child process uses isolated Python invocation (`-I -S`), a private
temporary working directory, no inherited user environment except basic
platform variables, and a **fixed** `STATUS`/`OBSERVE`/`THINK`/`STOP`
JSON-over-pipes protocol. It keeps explicitly included memory summaries as
inert state and can return IDs of relevant memory records without revealing
their text. There is no uploaded executable, arbitrary tool bridge, or host
effect. **Process separation is not an OS-enforced adversarial sandbox**:
there are no kernel-seccomp/container/VM guarantees or certified resource
limits, and the local caller's approval identity is only a declaration.
No claim of subjective continuity follows from copying software state.

## 4. Authentication and recovery before remote operation

The coordinator can explicitly opt into verified **worker-to-Director**
report/ack ingress. Supply `auth_channel=AuthenticatedChannel(auth_db,
{"worker_id": secret_bytes, ...})` to a *new* coordinator and call
`tick(auth_now_utc=...)` with bounded UTC POSIX seconds. The sender's
operator-configured symmetric HMAC-SHA256 key must contain 32–128 bytes;
generate and protect keys outside the repository. In this mode, ordinary
unsigned `MISSION_REPORT` and `MISSION_CANCEL_ACK` messages are rejected.

A worker creates a signed envelope with `channel.seal`:

The following snippet assumes `channel` is the explicitly constructed
`AuthenticatedChannel` holding the worker's configured key, `mailbox` is
the local `DirectorMailbox`, and `assignment_message_id` was read from the
assignment delivered to that worker. It is not a standalone deployment script.

```python
import time

now = int(time.time())  # caller-provided local clock, not independently trusted
raw = channel.seal(
    sender_id="worker_id", recipient_id="director", kind="MISSION_REPORT",
    payload={"in_reply_to": assignment_message_id,
             "body": {"finding": "requires independent review"}},
    issued_at=now, expires_at=now + 300,
)
mailbox.send(sender_id="untrusted_transport_label", recipient_id="director",
             kind="SIGNED_ENVELOPE", payload={"envelope": raw.decode("ascii")})
# director.tick(auth_now_utc=now)
```

The local mailbox label **does not** establish sender identity. Verification
checks signed sender, recipient, message type, TTL, nonce/replay ledger,
revocation, and expected assignment recipient before marking a report
`MATCHED_AUTHENTICATED_UNVERIFIED`. This confirms possession of a configured
key, **not** the truth of the report, human identity, action permission, or
worker cessation. A coordinator's auth mode is persisted and immutable;
historical unauthenticated reports cannot be relabeled as authenticated.

**This is not end-to-end secure communication.** Director-to-worker
assignments and cancellation notices remain *unsigned local mailbox
messages*. The payload is unencrypted. The supplied clock and key custody
are not independently certified. HMAC verification consumes a replay token
in a separate SQLite database before the coordinator commits the accepted
report; a crash between those commits can lose one valid message. Do not
claim exactly-once delivery or operate across hosts on this basis.

`RecoveryStore` independently checkpoints a bounded coordinator `inspect()`
snapshot plus optional restored software-agent data. An exact owner/domain
consent record controls checkpoint admission; a separate approval controls
the *data-only* restored receipt. SQLite makes each internal recovery commit
atomic; SHA-256 checks accidental corruption, not malicious modification by
a privileged local writer. `restore()` does **not** overwrite the live
coordinator database, start a worker, reinstall credentials or grant effects.
On POSIX, recovery/auth files must remain owner-accessible only (`0600`),
inside operator-controlled private directories; keep an independent backup
if recovery from disk loss matters.

## Current qualification boundary

Source tests cover graph progression, stale/contested completion evidence,
resource outage and deadlines, two-way persistent terminal conversation,
allowed data-only subprocess operations, HMAC tampering/expiry/replay,
operator consent, and local checkpoint recovery. Run:

```bash
python3 -B -m unittest discover -s tests -q
python3 tools/validate_hc_architecture.py --root .
```

These tests are evidence for local **software** behaviors only. They do not
establish human consciousness transfer, a channel to a hypothetical external
intelligence, omniscience, secure multi-device connectivity, or permission to
act on a host. Those remain separate research and qualification questions.
