# The Director — Local Foundations (v0.2 reference)

**Scope:** An offline, opt-in research reference that can bind an advisory decision to its complete inputs, persist selected records locally, coordinate analytical tasks across restarts, and reject unauthenticated effect requests. This is **not** an autonomous Director service, a network listener, an authenticated evidence system, or an effect executor.

These components deliberately remain separate:

| Component | Intended guarantee | Explicitly **not** guaranteed |
| --- | --- | --- |
| Complete decision dossier | Content-consistent, versioned, reproducible private snapshot of the mission, proposals, declared evidence, evaluation time, and existing decision | Truth of evidence, actor identity, trusted time, signature, or authorization |
| Local custody | Persisted, hash-linked local records with transactionally ordered append and integrity checks | Immutable storage against a privileged database writer, independent source attestation, or secret-safe retention without operator review |
| Task coordination | Durable local analytical task state, bounded retries, leases, and stale-worker fencing | Task execution, cross-host quorum, permission to act, or proof a stopped worker actually ceased |
| Authority reference | Fail-closed validation and rejection of unauthenticated authorization claims | A genuine independent host credential issuer, host-enforced grant, permission to perform an effect, or any effect itself |

The dependency direction is one-way: a caller can explicitly serialize a dossier and admit those bytes to custody; a caller can explicitly submit a **digest** as task input. A task lease cannot create or modify an authorization grant. No component launches a shell, worker, provider, external API, host agent, or deployment. No implicit ingestion of personal data or cross-repository state is permitted.

## Input and storage handling

1. The existing `runtime.director.evaluate()` remains recommendation-only. Its v0.1 receipt is a **summary**: different objectives or declared source references can produce identical summaries. Use a complete-input dossier when the identity of an advisory decision matters.
2. Full dossiers can contain private free text. They remain in memory until an operator intentionally serializes or persists them. Content digests can also expose guesses about low-entropy private inputs; publishing them is an explicit operator decision.
3. Local SQLite stores should use independent local files, exclusive write transactions, bounded records, explicit connection closure, and durable configuration. The hash link verifies internal consistency only: anyone who can rewrite all stored rows and recompute their hashes can counterfeit a consistent history.
4. On Lappy, VN1, and VN2, **do not share a writable SQLite WAL database over a network filesystem**. A real multi-host protocol would need an independently designed, authenticated, conflict-resolving owner boundary; this reference does not implement one.
5. Timestamps supplied by callers are not independently trusted. Task ownership and deadlines do not authenticate the operator or permit real-world effects.
6. Deletion, retention, backup, filesystem admission, credential protection, and separately enforced host grants remain operator/host responsibilities. Do not put secrets, personal histories, or identity claims into the stores merely because a field accepts bytes or text.

## Source ideas admitted by design, not by operational coupling

The local design adapts general patterns after inspecting the following pinned upstream sources. No runtime service, credentials, operational database, or proprietary module from these repositories is imported.

| Source commit | Pattern considered | Reuse boundary |
| --- | --- | --- |
| [`project-runner@44da26e`](https://github.com/thebrazenbeard/project-runner/tree/44da26e02c39591118bf1fdfe7ec01bd1103fc66) | SQLite `BEGIN IMMEDIATE` leasing and fencing | No inherited task dispatch or GitHub/worker routing |
| [`pre-active@e306c25`](https://github.com/thebrazenbeard/pre-active/tree/e306c251a4da44125ee58c311a35120084da8e4b) | Retry ceilings, durable pause/cancel state | No inherited scheduler or effect execution |
| [`voss@61fb97b`](https://github.com/thebrazenbeard/voss/tree/61fb97b8657db32fb73652df263336747a8980ed) | Evidence origins, rival claims, scope matching | No inherited identity or authenticated host grants |
| [`driftguard@0be6324`](https://github.com/thebrazenbeard/driftguard/tree/0be63249e9e3edd1456dc03ea68777315b60d0f6) | Deterministic fail-closed validation and digests | No inherited gate-as-authorization assumption |
| [`blotter@c31c93c`](https://github.com/thebrazenbeard/blotter/tree/c31c93ce2abbc8b491e9a9458fb1efa0bbf6e18a) | Local append log and hash-chain consistency | No inherited data, proof of truthful writers, or remote inbox |
| [`ccb-core@57b5bb9`](https://github.com/thebrazenbeard/ccb-core/tree/57b5bb97b923e43da0fb677b4e4a907bab222e37) | Payload identity and stale assignment rejection | No relay network or agent identity import |

## Release and activation gate

Source merge is limited to the deterministic, no-external-effect reference and its tests. Local databases are **not** automatically created by importing the package or starting a machine; their construction requires an explicit local caller and chosen path. A successful unit test does not qualify an authenticated host, and a passing internal authorization predicate cannot grant permission against an independently protected system.

Before any later effectful or network-exposed deployment, require a separate threat model and actual host-owned identity, policy, nonce/replay protection, revocation, effect receipt/reconciliation, privacy limits, cross-host coordination, and a substantiated **$0** operating path. Until that stage, all effect requests must remain denied. No Vercel, billing enrollment, card requirement, or paid-host fallback is part of this milestone.
