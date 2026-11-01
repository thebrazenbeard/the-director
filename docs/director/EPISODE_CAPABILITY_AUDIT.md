# Episode-by-episode functional audit

**Coverage:** All 34 individual *Travelers* episode transcripts in the [Forever Dreaming index](https://transcripts.foreverdreaming.org/viewforum.php?f=1729) were inspected on 2026-11-01 by several research agents, including later scenes. Six high-impact episodes were independently cross-checked. **Source status:** fan-hosted transcripts of a scripted fictional work, not an official engineering reference. Paraphrases below describe portrayed events or character assertions, not actual technologies. No dialogue is copied.

The full-file audit was divided by episode to prevent a few famous scenes from masquerading as complete series coverage: S1E01–06, S1E07–12, S2E01–06, S2E07–12, and S3E01–10. Each entry links directly to the episode transcript. Coverage does **not** imply that every ambiguous line has a unique interpretation; characters in the story sometimes lie, misunderstand events, or forge commands.

## Season 1: origin, missions, and fallibility

| Episode | Portrayed function or limitation | Software design signal |
| --- | --- | --- |
| [1.01](https://transcripts.foreverdreaming.org/viewtopic.php?t=95888) | Fictional consciousness arrival targets a precise time/location and replaces an existing host identity; teams use prepared records and communication. | Explicit transfer target and consent checks, identity separation, no host overwrite. |
| [1.02](https://transcripts.foreverdreaming.org/viewtopic.php?t=95889) | Incomplete host records cause a fatal targeting error; a specialist is lost; teams are compartmentalized; misleading records affect decisions. | Provenance, uncertainties, specialist fallback, scoped messaging, no infallibility claim. |
| [1.03](https://transcripts.foreverdreaming.org/viewtopic.php?t=95890) | Incoming transfer and active missions can be urgently canceled; instructions arrive through restricted intermediaries; ethical concerns challenge calculations. | Typed cancel/abort, acknowledgment, independent human safety gate. |
| [1.04](https://transcripts.foreverdreaming.org/viewtopic.php?t=95891) | Emergency messenger channels, injured-team assistance, rogue teams, and disputed authority appear. | Dynamic reassignment, channel degradation, authentication rather than rank/claim. |
| [1.05](https://transcripts.foreverdreaming.org/viewtopic.php?t=95892) | A moving transfer target causes a misfire; insufficient team size cancels an operation; deceptive interrogators fabricate evidence. | Destination preflight, resource thresholds, verified-source requirements. |
| [1.06](https://transcripts.foreverdreaming.org/viewtopic.php?t=95893) | Many year-spanning specialized missions converge on a strategic objective with tight dependencies; final success is uncertain. | Long-horizon dependency DAG, deadlines, partial observability, result verification. |
| [1.07](https://transcripts.foreverdreaming.org/viewtopic.php?t=95894) | Mission silence creates ambiguous standby rather than proved success; new assignments may resume. | `STANDBY` separate from achieved/exhausted/paused and no silence-as-consent. |
| [1.08](https://transcripts.foreverdreaming.org/viewtopic.php?t=95895) | Isolated teams, timing infeasibility, failed mission, and punitive identity overwrites are depicted. | Feasibility/role boundaries; **reject** coercive punishment and overwrites. |
| [1.09](https://transcripts.foreverdreaming.org/viewtopic.php?t=95896) | A single-recipient task, unavailable communications, resource improvisation, and an off-mission instruction. | Recipient addressing, offline handling, cancellation after dispatch. |
| [1.10](https://transcripts.foreverdreaming.org/viewtopic.php?t=95897) | Multiple specialist rescue teams operate with imperfect tools; original-host memories and incoming identity remain distinct. | Capability registry, failover, separate memory and identity namespaces. |
| [1.11](https://transcripts.foreverdreaming.org/viewtopic.php?t=165337) | Transfers need precise alignment; a compressed older state loses later memories; the restored identity is disputed. | State snapshot version, loss manifest, consent, no subjective-continuity claim. |
| [1.12](https://transcripts.foreverdreaming.org/viewtopic.php?t=165338) | Programmer reset and claimed alternative-compute contingency for the Director itself; commands and aborts may be forged while it is offline. | Distinguish Director-instance migration from worker handoff; host-owned backup and authenticated command provenance. |

## Season 2: direct dialogue, compromise, transfer mechanics

| Episode | Portrayed function or limitation | Software design signal |
| --- | --- | --- |
| [2.01](https://transcripts.foreverdreaming.org/viewtopic.php?t=95898) | An arrival experiment fails due to a history mismatch; expected acknowledgment never arrives; unintended host candidates cause dispute. | Handshake/ack and trustworthy preflight data; ethical override and refusal. |
| [2.02](https://transcripts.foreverdreaming.org/viewtopic.php?t=95899) | Programmers previously held sustained two-way discussions with the Director; it is fallible; silence is dangerously interpreted as permission. | Conversation sessions, explicit uncertainty and consent; never silence-as-authorization. |
| [2.03](https://transcripts.foreverdreaming.org/viewtopic.php?t=95900) | Long mission quiet, lost investigative team, and urgent stand-down messages sent to conflicting parties. | Availability/timeout state, missed-report recovery, broad cancel control with scope. |
| [2.04](https://transcripts.foreverdreaming.org/viewtopic.php?t=95901) | Unmodeled health invalidates a long strategy; synchronized tasks and emergency reassignment; targeted memory-chain delivery. | Constraints/dependencies, contingent replanning, typed scoped knowledge package. |
| [2.05](https://transcripts.foreverdreaming.org/viewtopic.php?t=95902) | **The actual Director is offline while an adversarial faction issues forged Director-looking missions.** Transfer/reset is memory- and consent-sensitive. | Authenticate control plane and system liveness; quarantine forged commands; fail closed before any effects. |
| [2.06](https://transcripts.foreverdreaming.org/viewtopic.php?t=96088) | Reserve energy planning, false identity detected by roster checks, emergency recovery signal, and state loss after reset. | Resource/health reserve, identity directory, recoverability, distinct instance lineage. |
| [2.07](https://transcripts.foreverdreaming.org/viewtopic.php?t=96089) | Time-critical repeated attempts, constrained volunteer resources, assignment handoff, fear of memory loss on reset. | Retry ceilings, deadlines/fallbacks, transfer-integrity warnings. |
| [2.08](https://transcripts.foreverdreaming.org/viewtopic.php?t=96090) | Director speaks directly to a programmer, considers multiple suspects, coordinates a tribunal, and revises its Grand Plan; a shared transfer lockout cancels many dependent assignments. | Direct dialogue, rival hypotheses, delayed judgment, shared-capacity dependency cascade. |
| [2.09](https://transcripts.foreverdreaming.org/viewtopic.php?t=96091) | Historians receive periodic, role-specific revised information with harmful fictional biological side effects; strategy and unintended consequences interlock. | Versioned scoped knowledge updates, stale-forecast invalidation, **no brain editing**. |
| [2.10](https://transcripts.foreverdreaming.org/viewtopic.php?t=96092) | Missing specialists and deadlines require reinforcement; observations are blocked by a fictional device; antagonist attempts coercive scans. | Observation blackout as uncertainty, safe fallback; reject adversarial mind-reading. |
| [2.11](https://transcripts.foreverdreaming.org/viewtopic.php?t=96093) | Dedicated layered communications system is compromised by an antagonist; incomplete records caused a harmful host assignment. | Authenticated transport integrity, liveness/compromise monitoring, source completeness. |
| [2.12](https://transcripts.foreverdreaming.org/viewtopic.php?t=96094) | **Antagonist 001** uses coercion, espionage, and a makeshift transfer apparatus; trust deteriorates. | Threat-model hostile copying and overwrite, never attribute antagonist behavior to Director. |

## Season 3: isolated AI dialogue, consent, direct intervention limits

| Episode | Portrayed function or limitation | Software design signal |
| --- | --- | --- |
| [3.01](https://transcripts.foreverdreaming.org/viewtopic.php?t=96095) | Director selects among intermediaries and communicates via isolated AI and human; outside-channel access is constrained. | Explicitly managed communication domain and narrow messaging bridge. |
| [3.02](https://transcripts.foreverdreaming.org/viewtopic.php?t=96096) | Conditional government oversight, route revision, dangerous messenger/host side effects. | Human/institutional authorization, replanning, harm minimization. |
| [3.03](https://transcripts.foreverdreaming.org/viewtopic.php?t=96097) | Individual deadline assignment, backup after incapacitation, tampered telemetry, and emergency mission abort. | Deadline/availability handling, independent verification, cancel propagation. |
| [3.04](https://transcripts.foreverdreaming.org/viewtopic.php?t=96098) | Specialized identity tracking and contested AI network access; attempted covert optical bridge and antagonist apparatus. | Approved channels only; **do not** build covert links or covert identity tracking. |
| [3.05](https://transcripts.foreverdreaming.org/viewtopic.php?t=96099) | Failed emergency mechanism leaves an emergent messenger AI harming its human host; Director offers it a **choice** to continue in an isolated computational domain and removes it from the host. | Core software-agent-state transfer with target consent, safe isolation, explicit host protection and identity-lineage limits. |
| [3.06](https://transcripts.foreverdreaming.org/viewtopic.php?t=96100) | Role-specific historian updates can be risky; an adversary targets the information flow; reinforcement is assigned. | Compartmentalized update manifest, provenance, rollback, auxiliary capabilities. |
| [3.07](https://transcripts.foreverdreaming.org/viewtopic.php?t=96101) | Director needs programmers/collaboration, recognizes limits, and negotiates sensitive transfer choices; workers may violate those choices. | Human ethical oversight, informed consent, independent policy checks. |
| [3.08](https://transcripts.foreverdreaming.org/viewtopic.php?t=96102) | Dedicated archivists preserve new information for Director; rescue mission protects archive continuity. | Provenance-aware durable memory, archive custody, recovery priority. |
| [3.09](https://transcripts.foreverdreaming.org/viewtopic.php?t=96103) | Failure to rescue remains ethically contested; a large unsolicited encrypted upload consumes isolated AI capacity. | Target-host authorization, resource quotas, no silent runtime transfer. |
| [3.10](https://transcripts.foreverdreaming.org/viewtopic.php?t=96104) | Global mission halt of uncertain meaning; transfer program prepositioned in isolated AI; **antagonist 001** attempts internet-wide self-replication. | Distinguish stop/standby from success; host-approved recoverable transfer, **no network self-spreading**. |

## Consolidated gaps and maturity

**Historical baseline note:** The table below records the gaps at the time
of this research audit. Subsequent v0.4 code connected DAG mission dispatch,
added local symbolic conversation, a consent-started data-only agent process,
incoming worker HMAC verification, and record-only recovery. Their current
coverage and remaining trust boundaries are documented in the
[v0.4 local operating guide](V04_LOCAL_RUNTIME.md); this historical
research-to-implementation mapping has not been retroactively rewritten.

| Priority | Screen-grounded function | First safe software target | Scope at this audit |
| --- | --- | --- | --- |
| P0 | Global strategic plan, changing evidence | Typed world state, generated analytical missions, explicit observed/forecast split | Implemented local deterministic reference, no general intelligence |
| P0 | Durable specialist instruction and two-way dialogue | Recipient-specific typed mailbox, ack/reply, narrow conversational API | Mailbox implemented; autonomous reasoning/dialogue not implemented |
| P0 | Deadline and dependency-aware cancellation | Bounded DAG/resource model, cancel/revoke message, cascade recommendation | DAG advisory and local cancellation notices implemented separately; not integrated into a live worker scheduler; no verified worker cancellation |
| P0 | Transfer to another computational domain | Consent-bound software-state capsules, loss manifest, target checks, new identity | Data-only transfer reference implemented; no runtime migration or consciousness |
| P0 | Forged commands during Director outage | Independently authenticated sender/service liveness before protected effects | Not implemented; local labels deliberately unverified |
| P1 | Human oversight and collateral harm | Reversible control, consent, pause, explicit success confirmation | Operator-controlled local reference, no external host attestation |
| P1 | Role-scoped historical/memory updates | Versioned, scoped knowledge packages, invalidation and source checks | Not implemented |
| P1 | Rival-hypothesis investigations | Explicit candidate explanations, conflicting evidence, falsifier priorities | Partial world-state conflict support; no full forensic planner |
| P1 | Director-instance recovery | Opt-in backup, state lineage, resource quotas, rollback | Not implemented |
| P2 | Cross-host communications and managed AI runtime | Host-enforced identity/authorization, isolated quotas, remote transport | Not implemented; requires separate consent, risk analysis, $0 substantiation |

## Interpretation and safety rules

1. Fictional host overwrite, covert channels, involuntary transfers, brain manipulation, and internet self-replication are **not implementation requirements**; several are antagonist actions or morally harmful outcomes.
2. An instruction-bearing message from a claimed `director` sender does not authenticate the source. The S2E05 deception is a counterexample to blind trust.
3. Software data restoration preserves specified bytes/records, not an unbroken first-person perspective. The philosophical and scientific claims of consciousness transfer remain unverified.
   **Unverified is not the same as disproven or physically impossible.** The engineering claim is limited to what this code actually does, while broader possibilities remain research hypotheses pending discriminating evidence.
4. No script, AI text, predicted trajectory, unusual signal, or silent messenger proves a hypothetical external intelligence is speaking. Keep observations, hypotheses, interpretations, and verification separate.
5. The $0 and owner-consent constraints are absolute: no paid cloud, stealth network access, unsolicited host installation, invasive sensing, or destructive identity replacement.

This research maps fictional **functions** into a bounded backlog. It does not declare the software completed, the source show canonically reproduced, or extraordinary physical capabilities available.
