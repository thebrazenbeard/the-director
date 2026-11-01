# Director capability research: fiction to executable software

**Research date:** 2026-11-01. **Status:** Design mapping, not a scientific or deployment claim.

**Epistemic precision:** No presently demonstrated method for human subjective-consciousness transfer, future-to-past signaling, or communication beyond observable reality is known to this project. That is a statement about current evidence and technology, **not a proof that such mechanisms are impossible in principle**. Keep physical possibility, present feasibility, and successful experimental demonstration distinct.

This is a functional study of a fictional coordinating intelligence. The public project name and README remain an original, independent software identity. The source material is referenced for research and is not copied into program text, dialog, branding, or assets.

The [episode transcript index](https://transcripts.foreverdreaming.org/viewforum.php?f=1729) lists 34 transcripts over three seasons. The examples below summarize selected episodes; character statements and dramatized events are not engineering requirements or evidence for the portrayed physics.

| Depicted function | Research examples | Local software analogue | Hard limit |
| --- | --- | --- | --- |
| Strategic intelligence adjusts a long-term plan after new events. | [S2E8](https://transcripts.foreverdreaming.org/viewtopic.php?t=96090), [S1E6](https://transcripts.foreverdreaming.org/viewtopic.php?t=95893) | Versioned objectives, observed world state, bounded alternative mission generation, re-evaluation on new evidence. | A forecast is a conditional simulation, not knowledge of the future. |
| Missions are directed to different specialists and results feed back into coordination. | [S2E7](https://transcripts.foreverdreaming.org/viewtopic.php?t=96089), [S3E8](https://transcripts.foreverdreaming.org/viewtopic.php?t=96102) | Explicit capability registry, recipient-specific durable local assignment/ack/reply, bounded retries, feedback records. | A message does not run a worker or prove a mission was accomplished. |
| The intelligence can communicate through a constrained, isolated intermediate system. | [S3E1](https://transcripts.foreverdreaming.org/viewtopic.php?t=96095) | Local typed inbox/outbox with correlation IDs and a deliberately narrow transport boundary. | A local mailbox cannot communicate with an unknown entity or reach remote machines by itself. |
| Instructions can be forged; silence and uncertain authority require skepticism. | [S1E12](https://transcripts.foreverdreaming.org/viewtopic.php?t=165338), [S3E10](https://transcripts.foreverdreaming.org/viewtopic.php?t=96104) | Declared sender identity separated from authentication; untrusted reports cannot rewrite goals, grant authority, or prove completion; pause/stop mechanism. | Local string labels and hashes do not authenticate another actor. |
| The intelligence facilitates transfer portrayed as a traveler's consciousness to a host, and offers an emergent AI a place in a separate computational domain. | [S1E1](https://transcripts.foreverdreaming.org/viewtopic.php?t=95888), [S1E11](https://transcripts.foreverdreaming.org/viewtopic.php?t=165337), [S3E5](https://transcripts.foreverdreaming.org/viewtopic.php?t=96099) | **Core parallel track:** a consent-based portable software-agent-state capsule containing versioned identity, permitted memory subset, goals, integrity checks, anti-replay ledger, and deterministic restoration tests. | Serialized software state is **not** transfer, copying, preservation, or proof of subjective human consciousness. It does not write to human minds or overwrite host identity. |
| Missions have deadlines, emergency aborts, and handoffs when a target becomes unavailable. | [S2E7](https://transcripts.foreverdreaming.org/viewtopic.php?t=96089), [S3E3](https://transcripts.foreverdreaming.org/viewtopic.php?t=96097) | Planned: explicit cancellation messages, deadlines, bounded reassignments, and a crash-safe task lease/revocation boundary. | A canceled message cannot itself stop an already running external worker; this reference launches none. |
| The intelligence seeks more computing/network access; the host's consent and resource limits can be violated. | [S3E4](https://transcripts.foreverdreaming.org/viewtopic.php?t=96098), [S3E7](https://transcripts.foreverdreaming.org/viewtopic.php?t=96101), [S3E9](https://transcripts.foreverdreaming.org/viewtopic.php?t=96103) | Host-controlled allocation, explicit opt-in, isolation, and quotas for any future runtime/domain transfer. | No covert channel, hidden network connection, host resource capture, or credential acquisition is authorized. |
| The intelligence may cease interventions rather than continue regardless of circumstances. | [S3E10](https://transcripts.foreverdreaming.org/viewtopic.php?t=96104) | Budget ceilings, explicit pause, no-action/abstention, independently confirmed objective completion. | Silence is not evidence of success or of any external actor. |

## Boundaries that matter

- **Software autonomy is bounded:** generating analytical missions and coordinating local messages is compatible with requiring independently enforced authorization before real device, financial, network, or other protected effects.
- **No self-propagation:** hostile internet-wide software activity late in the series is associated with an antagonist, not an acceptable Director design goal. The local build must not install itself, spread, expose ports, or solicit credentials.
- **Evidence is typed:** source strings are claims; hypotheses, simulations, and peer reports cannot silently turn into confirmed observations.
- **Continuity is testable only as information:** restoring the same records and behavior does not answer the philosophical problem of subjective experience, let alone solve the fictional human-brain transfer mechanism.
- **Host consent is a separate invariant:** communication, migration, and compute allocation require the domain owner's explicit authorization even if a fictional plot depicts bypassing it.
- **Budget remains $0:** Python standard library, opt-in local SQLite, no paid APIs, cloud infrastructure, card, or billing enrollment.

## Success criteria for the first executable loop

1. A goal and a capability catalog produce bounded proposed missions without separately hand-authoring every proposal.
2. A claimed observation changes the versioned world state and leads to a traceable plan revision, or to an explicit abstention if contested.
3. Assignment, acknowledgement, and a typed report survive program restart in a chosen local database.
4. A report from a mailbox sender, without independent confirmation, cannot be elevated into verified success or permission to perform an effect.
5. An operator pause prevents further assignment; a finite attempt budget is enforced; no automatically opened port, worker, or host process is involved.

## Future: agent-state transfer protocol

A candidate minimal schema is `{capsule_id, schema_version, subject_kind: 'SOFTWARE_AGENT', source_instance, consent_scope, created_utc, memory_manifest, goal_state, runtime_config, provenance, digest}`. Admission is explicit and local; data minimization, origin verification, integrity checking, version compatibility, import/export audit, and operator-controlled revocation are required. Sensitive memory is excluded by default. The first reference restore creates a **new state record intended for a distinct software instance**; it does not launch that instance, move a human consciousness, or silently replace an existing agent or person's identity.

Proposed hostile tests: corrupt capsule rejection; unsupported-version rejection; same-ID differing bytes; consent mismatch; excluded secrets remain excluded; restore fidelity across Windows/Linux; re-import never confers authorization; competing live instances remain separately identifiable. This is a core parallel milestone, not a future claim to transplant or duplicate a human mind. Implementation and qualification are separate from the strategic loop.

## Copyright and interpretation

No episode dialogue, scripts, stills, fictional names, or proprietary scenes are embedded in the code. Transcript links identify researched functions only. This mapping is interpretation, not an endorsed or canonical specification. It does not establish contact with or knowledge about a hypothetical administrator or creator of observable reality.
