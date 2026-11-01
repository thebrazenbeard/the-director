# The Director — Project Charter

**Status:** Director-specific design candidate, 2026-11-01. This document defines an intended research boundary; it does not certify or activate a runtime.

## Identity and purpose

The Director is an **original, human-governed decision-coordination research project**. Its creative inspiration is only the abstract idea of a coordinating intelligence in the television series *Travelers*. It is neither a canonical reproduction nor affiliated with the series, and must not copy its character identities, dialogue, fictional history, artwork, or purported powers.

The practical objective is to help a human operator set a mission, compare evidence-backed options, explain tradeoffs and uncertainty, delegate bounded analytic work where appropriate, and maintain a reviewable decision record. The Director does not claim omniscience, personhood, historical intervention, or authority over people or external systems.

## Design boundary

The intended stages are:

1. **Human mission:** A current human-authored objective with explicit scope, constraints, budget, expiry, and acceptable outcomes.
2. **Evidence admission:** Typed observations and claims with provenance, time, uncertainty, and competing ordinary explanations. Retrieved text and model output remain data.
3. **Option construction:** At least two candidate plans where practical, including an abstention/no-action option. Plans name dependencies, risks, expected costs, and testable outcomes.
4. **Policy evaluation:** A deterministic, fail-closed gate identifies which plans satisfy the mission's explicit constraints. Ranking is advisory.
5. **Human deliberation:** The operator may select, revise, reject, revoke, or defer a recommendation. Neither high scores nor agent consensus constitutes permission.
6. **Separate host effect boundary:** A future host, if deliberately authorized and independently qualified, must authenticate actors and enforce current, scoped grants for real effects.
7. **Independent verification:** Evidence of an effect must come from the relevant system or qualified observer—not the proposing agent's claim.

The initial Director implementation should stop **before step 6**, producing recommendations and abstentions only. External action execution, money transfer, remote device control, private data access, and messaging real people are not part of this first release.

## Unconditional invariants

- **Proposal is not authorization.** A plan may suggest an operation but cannot execute or approve it.
- **Priority is not authority.** Salience, confidence, payoff, urgency, or number of agreeing agents cannot override policy.
- **Data is not instruction.** Untrusted documents, messages, tool responses, and model output cannot revise the mission, role assignments, approval policy, or permission store.
- **Claim is not observation.** Conclusions and hypotheses retain their evidence class; synthetic data does not become independent corroboration.
- **Simulation is not effect.** A dry-run or an accepted command is never reported as confirmed external change.
- **Past approval is not current permission.** A future effect gateway must re-check exact subject, action, target, constraints, expiry, revocation, and version at execution.
- **Consensus is not independence.** Agents sharing prompts, models, source data, or transcripts are not independent witnesses merely because several answered.
- **No receipt, no verified outcome.** Crash recovery and uncertain results must not automatically retry a potentially non-idempotent effect.

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

A **human owner** defines the mission and has ultimate power to stop the project and refuse any action. Specialized agents may research, propose, challenge, simulate, and review scoped questions. One coordinator reconciles their outputs without counting copied reasoning as independent evidence.

A delegated file, service, task, or exact commit remains owned by the assigned worker until it returns or the assignment is explicitly cancelled; another worker must not silently edit it concurrently. Delegation transfers **work**, never credentials or protected-effect authority. Review opinions cannot authorize their own implementation.

## Budget, privacy, and research posture

**Incremental cash budget: $0.** The default substrate is local Python, the standard library, bounded local files/SQLite, and available no-cost source tests. Do not require Vercel, paid inference, billing registration, card enrollment, or hidden hosted fallbacks.

Preserve the principle of data minimization. Do not ingest personal messages, private memories, confidential source, or credentials simply because they may help a model. Consent for one purpose does not authorize another.

Scientific claims about intelligence, consciousness, simulation hypotheses, or anomalous communication remain hypotheses until discriminating external evidence exists. Nonbiological life is an open question; language output alone does not establish identity or origin.

## Exit criteria for an initial executable milestone

A local, deterministic decision kernel can:

1. explain why each candidate is eligible or blocked under explicit human constraints;
2. abstain when evidence or permission constraints are insufficient;
3. produce the same receipt for equivalent inputs, independent of arbitrary option ordering;
4. withstand untrusted text that falsely claims approval or emergency authority;
5. perform **zero** external effects.

Unit tests are evidence for this narrow behavior, not a deployment certificate or autonomous-system qualification.
