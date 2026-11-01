# The Director

**An original strategic-intelligence, mission-coordination, and software-agent continuity research project built from the God Brain source lineage.**

The Director is named for its intended function: working toward an operator-declared objective by maintaining a changing model of the world, constructing strategies, generating analytical missions, coordinating specialized agents, processing feedback, and deciding when to revise or stop. Its identity, scope, and constraints are defined by this project's own charter and tested contracts.

Its second core research direction is **continuity and transfer of software-agent state**: an explicitly consented handoff of permitted identity records, selected memory summaries, and goal parameters as a new software-state record. It does not itself launch an instance. This does **not** claim to transfer human consciousness, subjective experience, or biological minds.

**Autonomous planning is not unrestricted authority.** The Director may produce and coordinate local analytical tasks, but it is not an omniscient controller and may not independently intervene in people's lives, access hosts, obtain permissions, or spend money.

> **License:** The inherited source is visible but **not open source**; Patrick Sims's original material remains proprietary. Commercial use, redistribution, public derivatives, and hosted services require permission under [LICENSE](LICENSE) and [COMMERCIAL_LICENSE.md](COMMERCIAL_LICENSE.md). Existing third-party rights and [NOTICE](NOTICE) remain in force.

## Intended operating model

1. **Goal:** A human specifies a desired outcome and limits; subordinate analytical missions need not be hand-written.
2. **World state:** Bounded observations, hypotheses, and contradictions retain their declared origin and uncertainty.
3. **Strategy:** Local software generates and ranks candidate missions for registered analytical capabilities, labeling predictions as simulations.
4. **Coordination:** A durable, recipient-specific local mailbox carries assignments, acknowledgments, and replies. Reports stay unverified; newly operator-admitted observations trigger replanning.
5. **Continuation:** An opt-in capsule can produce a permitted software-agent-state record for a distinct instance, subject to consent and integrity checks. It does not start that instance.
6. **Control:** Explicit pause, attempt ceilings, independent outcome confirmation, and separate host authority apply. A planned or reported effect is never automatically a verified result.

Governing distinctions:

- Proposal is not permission.
- Priority is not authority.
- Simulation is not observation.
- A request sent is not an effect confirmed.

The Director may autonomously **propose and route analytical missions inside an explicitly started local research loop**. **It cannot mint credentials, approve itself, execute shell commands, spend money, access private material, launch agents, or command another system merely by claiming priority or intelligence.**

The Director-specific [project charter](docs/director/CHARTER.md) gives the design boundaries. The [34-episode capability audit](docs/director/EPISODE_CAPABILITY_AUDIT.md) and [research-to-software mapping](docs/director/SCREEN_FUNCTION_TO_SOFTWARE.md) record the source-grounded functional analysis and technical limits, without adopting fictional powers or branding in this README. The [source lineage map](docs/director/LINEAGE_AND_SCOPE.md) explains what the import does and does not inherit; the [portfolio admission register](docs/director/PORTFOLIO_ADMISSION.md) separates reusable ideas from approved code.

## What is implemented today

This repository was initialized from the exact Git history of [God Brain](https://github.com/thebrazenbeard/god-brain) `main` at commit [`7eec60b`](https://github.com/thebrazenbeard/god-brain/commit/7eec60b93b6d355f4c50e5bcb4cc23a19eb2291c) on 2026-11-01. The imported code is a research substrate—not a finished Director runtime.

| Source | Tested capability in its original scope | What it does **not** establish |
| --- | --- | --- |
| [Director world state](runtime/director/world_state.py) | Versioned typed evidence, provenance claims, conflict preservation, restartable snapshots | Independent verification of evidence sources or a sensor connected to reality |
| [Director strategy generator](runtime/director/strategy.py) | Deterministic generation of bounded analytical missions from a high-level goal, a capability catalog, and observations | Future knowledge, causal optimization, a pretrained AI, or worker execution |
| [Director mission graph](runtime/director/mission_graph.py) | Independent DAG scheduling reference with shared resource capacities, deadlines, and cancellation cascade recommendations | **Not yet wired into the durable coordinator**, no actual worker/resource scheduler |
| [Director coordinator](runtime/director/coordinator.py) | Explicit local `tick()` producing missions, relaying reports, tracking evidence and outcome confirmation across restart | Self-running AGI, an external effect runner, verified remote identities, or automatic achievement |
| [Director messenger](runtime/director/messenger.py) | Persistent local recipient-specific SQLite messages, acknowledgement, correlation and replies | Authentication, secure cross-host transport, or a worker actually performing a task |
| [Director stop policy](runtime/director/stop_policy.py) | Operator pause, bounded attempts, unresolved or exhausted states, and guarded success claims | Independently proven success or host authority |
| [Software-agent state capsules](runtime/director/agent_state.py) | Explicitly scoped, integrity-checked local data export/restore into new state records with durable replay protection | Human consciousness transfer, process startup, author authentication, or subjective identity continuity |
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

**Current claim ceiling:** A source-imported foundation and a bounded, opt-in **same-host analytical coordination prototype**: it can construct missions from registered goals/capabilities, persist and route messages, read declared feedback, revise analytic priorities, and preview or restore software-agent data records. It is **not self-running**, does not start workers or models, cannot authenticate sources, and has no qualified remote Director service, live multi-host coordinator, human-mind transfer, or protected-effect deployment.

## How it works today

The Director runs as an **importable Python reference implementation**, not a background service. Its newer loop differs from the original evaluator: a caller defines a **high-level goal and registered analytical capabilities**, then calls `DirectorCoordinator.tick()` to generate and route candidate missions. A separate local software worker (if one is deliberately provided) may poll its mailbox, acknowledge an assignment, and send a reply. The coordinator records that reply as an **unverified report**; only a distinct, operator-reviewed observation can establish a new world-state fact or claim completion. No API calls, agents, listeners, or external actions start automatically.

Run the harmless local cycle from the repository root:

~~~bash
python3 -m examples.director_local_cycle
~~~

The example creates temporary local databases, demonstrates a generated assignment to a **simulated** worker, an acknowledgement and reply, and separate operator confirmation of a sample metric. It closes and removes its test databases. It is a protocol demonstration, not a live AI or a claim that the assignment changed anything outside the program.

The opt-in `export_agent_state()` / `restore_agent_state()` API separately prepares and restores **selected software state**, with consent, bounded memory, a target domain, a new instance identifier, checksum verification, and persistent anti-replay checks. It returns data; it neither launches the restored software agent nor migrates human consciousness. The [research mapping](docs/director/SCREEN_FUNCTION_TO_SOFTWARE.md) tracks which additional capabilities still need building.

The original v0.1 decision kernel is also available. It accepts fully caller-supplied candidate actions and evaluates them **as advice only**, independently of the newer strategic loop.

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

The evidence origin in that legacy evaluator example is a **caller-supplied claim**, not independently verified. `build_dossier()` returns the full private input-bound record in memory. You can explicitly serialize it, persist it with the opt-in local custody store, or submit its digest to the task ledger. None of those steps authorizes or executes the recommended action.

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
python3 -m unittest -q \
  tests.test_director_world_state \
  tests.test_director_strategy \
  tests.test_director_mission_graph \
  tests.test_director_messenger \
  tests.test_director_stop_policy \
  tests.test_director_coordinator \
  tests.test_director_agent_state
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

1. **Interdependent missions:** Connect the now-tested standalone dependency/resource graph to the coordinator's durable assignments and cooperative cancellation messages; add graph-versioned replanning and deadline delivery. A graph library alone is not a live scheduler.
2. **Director dialogue:** Support genuine, bounded two-way discussion with specialized software agents and an operator, while establishing and verifying which local system is allowed to speak as the coordinator. Current local sender IDs are only claims.
3. **State-transfer qualification:** Destination preflight, explicit loss/omission manifest, record-only dry-run, source/destination consent, and later crash-recoverable activation with host resource limits. A restored data record is not a running intelligence.
4. **Cognition and grounding:** Explore grounded scenario models, rival hypotheses, role-scoped knowledge updates, model revision, and local no-cost inference only when actually available. Existing deterministic components are **not** a pretrained general intelligence or a consciousness substrate.
5. **Control and recovery:** Independent evidence/source authentication, information minimization, safe coordinator backup, rollback/revocation, and hostile-state review **before** any remote or effectful operation. No protected action is authorized by a reference test.

This milestone is **a tested analytical coordination skeleton and software-state capsule**, not a finished general-intelligence service or a real-world consciousness-transfer device. The [research-to-software map](docs/director/SCREEN_FUNCTION_TO_SOFTWARE.md) records both implemented mechanisms and known omissions.

## Boundaries and cost

- **Absolute incremental budget: $0.** No Vercel, billing enrollment, card requirement, paid model API, or paid hosting fallback. Favor local Python and free repository checks.
- Preserve human consent and privacy. Inputs and model outputs are **not** credentials, authenticated source identity, or proof of external agency.
- Stores and opt-in state capsules contain plaintext local data. Selected memory summaries may include secrets even if there is no explicit credentials field; **redact before export**. Capsule checksums are not signatures, and restartable message logs are not a trusted remote transport.
- No protected effects without an independently enforced host authority boundary; no autonomous deployment or user-data ingestion merely because a component can propose it.
- Treat artificial life and nonbiological consciousness as open questions, but generated text or surprising behavior is not evidence of origin or identity.

The Director is a new project with its own future specification and qualification path. **God Brain remains a distinct repository and research project**; inherited mechanisms remain attributed to their actual sources.
