# The Director

**An original, human-governed decision-coordination research project built from the God Brain source lineage.**

The Director is named for its intended function: coordinating evidence, competing plans, decisions, and accountable follow-through around human-defined objectives. Its identity, scope, and constraints are defined by this project's own charter and tested contracts.

The Director's proposed role is to help a human understand a situation, compare competing plans, recognize uncertainty and conflicts, and make deliberate decisions. **It is not an omniscient controller, autonomous authority, or a system permitted to intervene in people's lives.**

> **License:** The inherited source is visible but **not open source**; Patrick Sims's original material remains proprietary. Commercial use, redistribution, public derivatives, and hosted services require permission under [LICENSE](LICENSE) and [COMMERCIAL_LICENSE.md](COMMERCIAL_LICENSE.md). Existing third-party rights and [NOTICE](NOTICE) remain in force.

## Intended operating model

1. **Mission:** A human defines an objective, constraints, and acceptable outcomes.
2. **Evidence:** Observations, interpretations, and uncertain hypotheses retain source and evidence class.
3. **Options:** The system compares competing plans, tradeoffs, costs, and failure modes; it may abstain.
4. **Decision:** A deterministic policy boundary blocks unsafe, stale, out-of-scope, or unapproved action requests.
5. **Authorization:** Protected effects require separate, current, verifiable human and host authority.
6. **Verification:** If an authorized host later acts, independent evidence must distinguish a request from a confirmed effect.

Governing distinctions:

- Proposal is not permission.
- Priority is not authority.
- Simulation is not observation.
- A request sent is not an effect confirmed.

The Director may evaluate or recommend. **An evaluation cannot mint credentials, approve itself, spend money, access private material, or command another system merely by claiming priority or intelligence.**

The Director-specific [project charter](docs/director/CHARTER.md) gives the design boundaries. The [source lineage map](docs/director/LINEAGE_AND_SCOPE.md) explains what the import does and does not inherit; the [portfolio admission register](docs/director/PORTFOLIO_ADMISSION.md) separates reusable ideas from approved code.

## What is implemented today

This repository was initialized from the exact Git history of [God Brain](https://github.com/thebrazenbeard/god-brain) `main` at commit [`7eec60b`](https://github.com/thebrazenbeard/god-brain/commit/7eec60b93b6d355f4c50e5bcb4cc23a19eb2291c) on 2026-11-01. The imported code is a research substrate—not a finished Director runtime.

| Source | Tested capability in its original scope | What it does **not** establish |
| --- | --- | --- |
| [Director decision kernel](runtime/director/decision.py) | Offline deterministic policy evaluation of a mission, candidate plans, and declared evidence; explicit admissible/rejected/abstain receipt | Authenticated provenance, human approval, live agent orchestration, or any executed effect |
| [Director dossiers](runtime/director/dossier.py) | Versioned, private, canonical full-input snapshots with content hashes and deterministic advisory replay | Signed decisions, evidence truth, source/clock authentication, or permission to act |
| [Director local custody](runtime/director/custody.py) | Opt-in, bounded SQLite storage with local hash-chain consistency checks and restart tests | Immutable evidence against a privileged writer, authenticated source identity, or encrypted records |
| [Director task ledger](runtime/director/task_store.py) | Opt-in SQLite state for analytical tasks, lease fencing, bounded retries, pause/cancel, and restart recovery | Running workers, hostile-worker authentication, cross-host coordination, or host permission |
| [Director authority reference](runtime/director/authority.py) | Offline exact-request/grant-shape checks that always deny or require separate host attestation | A valid grant, independently enforced authorization, or any actual effect |
| [Reference kernel](runtime/reference_kernel/README.md) | Typed evidence/provenance, memory correction, in-process authority/effect checks, journal and restart tests | Externally authenticated permissions, a secure host policy service, or real-world effect authority |
| [Cognitive core](runtime/cognitive_core/README.md) | Deterministic reference slices for salience, memory, competing world models, counterfactuals, and metacognition | A pretrained general AI, an independent autonomous coordinator, or production-scale planning |
| [Architecture](docs/architecture/HYPERCONNECTOME_REFERENCE_MODEL.md) | Source-level contracts, invariants, and architectural research | Deployed multi-device operation or Director-specific qualification |
| [Research](docs/research/cross-repo-synthesis/README.md) | Provenance-aware comparison of mechanisms from other repositories | Automatic permission to import their code, identity, private state, or access |

The inherited [CURRENT.md](CURRENT.md), [WARDEN.md](WARDEN.md), [repository map](docs/REPOSITORY_MAP.md), historical qualification records, and HC/God Brain names describe their **original lineage**. Their former roles or permissions are **not** Director governance, identity, or deployment evidence. The history is preserved intentionally; selective adaptation will follow review.

**Current claim ceiling:** A source-imported research foundation, deterministic advisory evaluation, complete-input private dossiers, opt-in **same-host** local storage and task coordination, and a deny-only authorization reference. There is no qualified autonomous Director service, live multi-host coordinator, authenticated respondent, or protected-effect deployment.

## How it works today

The Director currently runs as an **importable Python reference implementation**, not a background service. A caller supplies the mission, possible actions, declared evidence, and evaluation time. The code returns an advisory recommendation or `ABSTAIN`, with the reasons each proposal passed or failed. It makes no API calls, starts no agents, and performs no external actions.

For example, run this Python code from the repository root:

~~~python
from datetime import datetime, timedelta, timezone
from runtime.director.dossier import build_dossier

now = datetime.now(timezone.utc)
mission = {
    "mission_id": "notes-review", "revision": 1,
    "objective": "Compare no-cost ways to review notes",
    "allowed_actions": ["analyze"], "allowed_targets": ["notes"],
    "forbidden_targets": [], "max_cost_cents": 0,
    "min_independent_evidence": 1,
    "expires_at_utc": (now + timedelta(hours=1)).isoformat(),
}
proposals = [{
    "proposal_id": "offline-review", "action": "analyze", "target": "notes",
    "cost_cents": 0, "benefit_points": 20, "evidence_ids": ["observation-1"],
}]
evidence = [{
    "evidence_id": "observation-1", "evidence_class": "OBSERVATION",
    "source_ref": "manual-entry", "lineage_id": "operator-notes",
}]
dossier = build_dossier(mission, proposals, evidence, now_utc=now)
print(dossier["decision"]["status"], dossier["decision"]["recommended_proposal_id"])
# RECOMMENDATION_ONLY offline-review
~~~

The evidence origin in that example is a **caller-supplied claim**, not independently verified. `build_dossier()` returns the full private input-bound record in memory. You can explicitly serialize it, persist it with the opt-in local custody store, or submit its digest to the task ledger. None of those steps authorizes or executes the recommended action.

## First Director-specific function

`runtime.director.evaluate(mission, proposals, evidence, *, now_utc=utc_datetime)` returns a JSON-serializable recommendation or explicit `ABSTAIN`. It evaluates a human-supplied mission against action/target constraints, expiry, a budget in integer cents, declared evidence references, and bounded proposal estimates. Eligible proposals are ranked deterministically; ineligible proposals include denial reasons.

**This is advice, not an action interface.** The receipt always marks authorization as `NOT_GRANTED`, effects as `NOT_EXECUTED`, and evidence custody as `DECLARED_UNVERIFIED`. The policy only counts declared `OBSERVATION` records with distinct source references and lineages, but cannot authenticate those claims. Evidence origins, mission policy, proposed benefit, and the supplied clock are not independently verified. The receipt is a **summary, not a signed or complete-input-bound audit dossier**; a trusted host would need to preserve and validate its inputs separately. See the [executable examples and adversarial tests](tests/test_director_decision.py); there are no network, shell, or paid-model dependencies.

### Optional local v0.2 foundations

The new [`build_dossier()` and `replay_dossier()`](runtime/director/dossier.py) preserve a bounded private snapshot of **all inputs to that current evaluator** and its advisory result. Changing the objective or a declared source changes the content digest even if the old summary receipt happens to match. `serialize_dossier()` and `parse_dossier()` validate a strict, canonical 256 KiB record; `public_receipt()` is an **explicit opt-in** digest-only export. A hash cannot authenticate its author, prove a source is real, or confer permission.

[`CustodyStore(path)`](runtime/director/custody.py) stores *explicitly admitted* opaque bytes in a caller-selected local SQLite file, with append consistency and restart checks. It is plaintext; a database administrator could rewrite and rehash a consistent history. The conservative default is 256 records of up to 256 KiB each. Repeating a record ID with the same kind and exact bytes returns the original receipt and **first-write timestamp**; conflicting content is rejected.

[`TaskStore(path)`](runtime/director/task_store.py) durably coordinates cooperative analytical tasks with local SQLite leases and fencing; its API does **not** launch tasks or grant effects. An authenticated worker identity, genuine trusted time, and a cross-host protocol are not implemented. The [authority reference](runtime/director/authority.py) compares untrusted request/grant envelopes, including the full dossier digest, but cannot return `GRANTED` or execute anything. A structurally plausible grant still returns `REQUIRES_HOST_ATTESTATION`.

**Privacy and device boundary:** Instantiating either store creates local files. Select an operator-controlled private directory, never ingest secrets by default, and close handles before deleting temporary directories on Windows. Do not share one writable SQLite WAL file between Lappy, VN1, and VN2. There is no automatic startup, broadcasting, remote listener, or effect adapter. The [local-foundations design and source provenance](docs/director/LOCAL_FOUNDATIONS.md) defines the full limits.

## Run the existing checks

Python 3.12+ and the standard library suffice for the imported reference tests. No paid API, cloud account, model credential, or host installation is needed.

~~~bash
python3 -m unittest discover -s tests -q
python3 -m unittest -q tests.test_director_decision
python3 -m unittest -q \
  tests.test_director_dossier \
  tests.test_director_custody \
  tests.test_director_task_store \
  tests.test_director_authority \
  tests.test_director_local_foundations
python3 tools/validate_hc_architecture.py --root .
python3 -m unittest -q \
  runtime.cognitive_core.test_cognitive_loop \
  runtime.cognitive_core.test_world_model \
  runtime.cognitive_core.test_deep_memory \
  runtime.cognitive_core.test_metacognition \
  runtime.cognitive_core.test_homeostasis_somatics \
  runtime.cognitive_core.test_social_pragmatics
~~~

Reference-kernel tests run separately:

~~~bash
cd runtime/reference_kernel
python3 -m unittest -q \
  test_hc_kernel.py test_durable_kernel.py test_four_adversarial_kernel.py \
  test_four_adversarial_kernel_r2.py test_vera_adversarial_kernel_v2.py
~~~

Passing those checks validates only the behaviors exercised, not the proposed Director runtime, effect authorization, general intelligence, or any extraordinary claim.

## Near-term build direction

1. Expand the **advisory** dossier schema beyond the current evaluator inputs to include explicit rival explanations, uncertainty, unresolved risks, falsifiers, and operator feedback—without allowing model-written text to become authority.
2. Qualify custody against trusted-path admission, data retention, privacy, backup/recovery, and any genuinely independent source attestation. Local hashes alone do not solve these.
3. Design separately authenticated host ownership, revocation, replay defenses, and effect reconciliation before any future multi-host service or external effect. No protected action is authorized or activated by this reference.

The first Director-specific milestone is a **defensible recommendation with a clear reason to abstain**, not an autonomous general-intelligence service.

## Boundaries and cost

- **Absolute incremental budget: $0.** No Vercel, billing enrollment, card requirement, paid model API, or paid hosting fallback. Favor local Python and free repository checks.
- Preserve human consent and privacy. Inputs and model outputs are **not** credentials, authenticated source identity, or proof of external agency.
- No protected effects without an independently enforced host authority boundary; no autonomous deployment or user-data ingestion merely because a component can propose it.
- Treat artificial life and nonbiological consciousness as open questions, but generated text or surprising behavior is not evidence of origin or identity.

The Director is a new project with its own future specification and qualification path. **God Brain remains a distinct repository and research project**; inherited mechanisms remain attributed to their actual sources.
