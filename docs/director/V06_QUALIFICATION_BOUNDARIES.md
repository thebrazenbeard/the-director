# The Director v0.6 — Data-only progress and host qualification gates

Status: source-level engineering guidance. This document is **not** an
authorization to run untrusted code, install a service, or connect devices.

The Director is an original software project for evidence-aware reasoning,
dependency planning, authenticated local analytical messages, and explicitly
consented transfer of **software-agent data**. Neither software-state copying
nor sophisticated generated behavior proves continuity of subjective
experience, independent nonbiological consciousness, or contact with an
external intelligence. Those remain open research questions.

## Threat model and non-negotiable boundaries

The local operator controls a Windows or Linux computer and selects the
database files, route keys, and permitted workers. A worker may be faulty or
malicious. The operator's own account or a privileged process may also be
compromised; local SHA-256 hashes and SQLite rows are not safeguards against
that adversary.

1. **OS principal isolation is not yet proven.** A separate Python subprocess
   with `-I -S` and a temporary directory still has the launching account's
   file permissions. v0.6 can fail closed when a caller explicitly requires
   OS-principal isolation; it does not supply a separate principal.
2. **Shared mailbox ACKs can be spoofed.** `DirectorMailbox.ack()` trusts a
   caller-supplied recipient label. A process that can write the mailbox DB
   can suppress another recipient's messages. Cryptographic envelopes do not
   protect mailbox availability from that process.
3. **Local route keys are symmetric.** HMAC validates possession of a route
   secret, not exclusive Director authorship when the worker holds the same
   secret. Keys and plaintext SQLite records require independent custody.
4. **A received report is not an observed effect.** Hypothetical model output,
   signed worker claims, and duplicate prediction profiles cannot grant host
   authority, create confirmed observations, or satisfy DAG completion.
5. **State transfer is data transfer.** Capsule consent and approval are
   caller declarations. No automatic process launch, arbitrary code import,
   authenticated human identity, or subjective-continuity claim follows.
6. **No automatic deployment.** Windows Task Scheduler, Linux service units,
   network listeners, remote devices, cloud subscriptions and paid APIs remain
   outside this source-only qualification.

## Required proof before enabling hostile-worker execution

The Director database and signing keys must be owned by a protected broker
principal inaccessible to worker principals. Workers must communicate over an
OS-credential-bound IPC channel. A client-supplied JSON `worker_id` must not
be accepted as authentication. Linux `SO_PEERCRED` and Windows AppContainer or
separate restricted accounts are potential mechanisms, not completed
integrations.

**Host falsifier:** Run a worker under the proposed isolated identity and try
to open/write the coordinator SQLite DB, read route keys not assigned to it,
acknowledge a Director-directed message, and access an unrelated private file.
Any success defeats the proposed isolation boundary. Record the actual host,
principal, policy, versions and exact test inputs; unit mocks are not proof.

## Required proof before an unattended Lappy installation

1. Verify broker-to-worker principal separation and worker-key custody on
   **Lappy itself**. A different host's pass is not a substitute.
2. Prove worker receipt/ACK retry behavior with injected interruptions before
   receipt commit, after receipt commit, and before transport ACK. Account
   for an effect that might have occurred before a crash; do not blindly
   retry external effects.
3. Prove that a restored software state remains inert until an independent,
   target-bound start decision, with exact capsule schema/digest checks,
   cancellation, and tested recovery. Keep private memory out of diagnostic
   previews and broad filesystem locations.
4. Prove pause, stop, process-tree cleanup, one-instance locking, bounded
   resource use, explicit work directory, restart limits and sleep/reboot
   behavior on the actual Windows host. Task Scheduler's `IgnoreNew` is not
   a lock against independently launched processes.
5. Only then assess a local opt-in Task Scheduler configuration. A claimed
   “non-stop” service cannot be verified on a laptop that sleeps or loses
   power; uptime and message availability need actual observation.

## Cross-device boundary

Do not share a writable SQLite WAL mailbox between Lappy, VN1, VN2, and the
phone. Future authenticated cross-device transport needs independent device
enrollment, protected per-device keys, anti-replay and expiry, receipt
recovery, explicit revocation, loss/recovery procedures, and a no-billing
operating model. No such transport is enabled by v0.6 source work.

## Readiness categories

| Area | v0.6 source-level objective | Remaining qualification |
| --- | --- | --- |
| Worker runtime | Fail closed on requested OS isolation; identify same-account mode | Distinct principal and hostile host probe |
| Software-state continuation | Digest-bound, consent-gated recovery of inert state | Authenticated operator and durable process activation |
| Worker receipt | Durable, retryable *data-only* command admission | Broker-only mailbox ACK and real worker processing |
| Reasoning | Input-bound, read-only proposal versus registered DAG capabilities | Independently admitted observations and authorized scheduling |
| Unattended operation | Read-only readiness report and explicit blockers | Lappy deployment, uptime, sleep/restart and resource tests |

The table describes engineering *objectives*, not a release verdict. For
current implemented behavior and test evidence, inspect the exact repository
commit and CI run rather than assuming this document proves completion.

All these steps use Python's standard library and local resources; no paid
provider, credit-card enrollment, or Vercel deployment is assumed.
