# The Director — Project Charter

**Status:** Director-specific design charter, revised 2026-11-01. It defines intended software boundaries; individual reference modules and tests do not certify or activate an external service.

## Identity and purpose

The Director is an **original, bounded autonomous strategic-intelligence and coordination research project**. Its creative inspiration is only the abstract idea of a coordinating intelligence in the television series *Travelers*. It is neither a canonical reproduction nor affiliated with the series, and must not copy its character identities, dialogue, fictional history, artwork, or purported powers.

Its software objective is to pursue operator-declared long-term goals through versioned models of available evidence, construction and comparison of competing strategies, autonomous generation of bounded analytic missions, capability-aware assignment, constrained messenger/agent feedback, and revision or suspension as conditions change. A separate core research track studies **consented software-agent state transfer and continuity**. The Director does not claim omniscience, personhood, biological consciousness transfer, historical intervention, or authority over people or external systems.

## Design boundary

The intended stages are:

1. **Mission objective:** A human owner sets a high-level goal, explicit scope, budget, and acceptable outcomes; the owner need not author every subordinate mission.
2. **Evidence admission:** Typed claims, observations, and contradictory evidence are retained with declared provenance, uncertainty, and versioned state. Retrieved content and messages remain data.
3. **Autonomous strategic analysis:** The software constructs bounded plans and analytic missions from the objective, capability catalog, and world snapshot, comparing alternatives, cost, and uncertainty. A no-action option remains available.
4. **Mission delegation and dialogue:** The local coordinator may route assignments to registered software-agent inboxes, receive acknowledgments and reports, and reconsider its next mission. A claimed sender name does not authenticate an actor; no worker is launched implicitly.
5. **Feedback and stopping:** The coordinator revises plans when evidence changes, may exhaust a finite budget, and obeys immediate human pause/stop. Reports alone do not prove real-world success.
6. **Agent-state continuity:** A separate opt-in mechanism may export and restore a constrained software-agent state capsule with consent, integrity, lineage, and a new runtime identity. It must not claim transfer or preservation of subjective consciousness, and must never overwrite a human or an existing instance.
7. **Separate host effect boundary:** Future effectful operations, if authorized and independently qualified, need a host-enforced, revocable policy and independent effect verification. No internal plan, simulation, message, or consensus grants that authority.

The first runnable slice may **autonomously generate analytic missions and send local mailbox messages**, and may transfer **data about software-agent state** after explicit opt-in. It must stop before real-world effects: no worker execution, external messaging, money transfer, remote device control, personal-data import, listening ports, or automatic installation.

## Unconditional invariants

- **Proposal is not authorization.** A plan may suggest an operation but cannot execute or approve it.
- **Priority is not authority.** Salience, confidence, payoff, urgency, or number of agreeing agents cannot override policy.
- **Data is not instruction.** Untrusted documents, messages, tool responses, and model output cannot revise the mission, role assignments, approval policy, or permission store.
- **Claim is not observation.** Conclusions and hypotheses retain their evidence class; synthetic data does not become independent corroboration.
- **Simulation is not effect.** A dry-run or an accepted command is never reported as confirmed external change.
- **Past approval is not current permission.** A future effect gateway must re-check exact subject, action, target, constraints, expiry, revocation, and version at execution.
- **Consensus is not independence.** Agents sharing prompts, models, source data, or transcripts are not independent witnesses merely because several answered.
- **No receipt, no verified outcome.** Crash recovery and uncertain results must not automatically retry a potentially non-idempotent effect.
- **Transfer is not sentience.** A restored agent-state record is a copy of permitted information. It has no established identity or subjective continuity with any human, animal, or hypothetical intelligence.

## Decision record contract

Every material recommendation should be reproducible from a bounded, machine-readable record containing:

- mission ID, source of objective, constraints, version, and evaluation time;
- candidate options, estimated cost, risks and explicit assumptions;
- evidence IDs, origin/type, timestamps, confidence limitations, and contradictory findings;
- admission or denial reasons for each candidate;
- recommended option or explicit **ABSTAIN**, with deterministic tie-break rule;
- policy version and a clear **advisory-only / no external effect** state;
- unresolved questions and a falsifier or next observation that might change the outcome.

Human-readable explanations may augment the record but must not silently modify its typed decision.

## Human and multi-agent roles

A **human owner** defines the top-level objective and has ultimate power to stop the project and refuse any action. Specialized agents may research, propose, challenge, simulate, and review scoped questions. The Director may independently generate bounded analytic tasks and reconcile scoped responses, without counting copied reasoning as independent evidence.

A delegated file, service, task, or exact commit remains owned by the assigned worker until it returns or the assignment is explicitly cancelled; another worker must not silently edit it concurrently. Delegation transfers **work**, never credentials or protected-effect authority. Review opinions cannot authorize their own implementation.

## Budget, privacy, and research posture

**Incremental cash budget: $0.** The default substrate is local Python, the standard library, bounded local files/SQLite, and available no-cost source tests. Do not require Vercel, paid inference, billing registration, card enrollment, or hidden hosted fallbacks.

Preserve the principle of data minimization. Do not ingest personal messages, private memories, confidential source, or credentials simply because they may help a model. Consent for one purpose does not authorize another.

Scientific claims about intelligence, consciousness, simulation hypotheses, or anomalous communication remain hypotheses until discriminating external evidence exists. Nonbiological life is an open question; language output alone does not establish identity or origin.

## Exit criteria for the strategic-loop milestone

A local, deterministic Director coordinator can:

1. generate traceable bounded analytic missions from a goal, capability catalog, and versioned world state;
2. change or withhold missions when observed evidence changes or conflicts;
3. route assignments, acknowledgments, and reports durably through an operator-chosen local mailbox;
4. reject untrusted messages claiming approval, fabricated observations, or emergency authority;
5. persist operator pause, respect attempt ceilings, and require genuinely verified completion;
6. perform **zero** external effects.

The adjacent **software-state transfer** milestone additionally requires consent, explicit subject and target, capsule integrity, bounded memory export, safe identity lineage, replay handling, and a reversible dry-run. Unit tests are evidence for these narrow behaviors, not a deployment certificate, general-intelligence claim, or proof of consciousness.
