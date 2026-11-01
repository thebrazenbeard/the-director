# EXP-SETI-NEG-01 — synthetic signal / interference benchmark

Status: **FROZEN PRE-REGISTRATION — 2026-11-01, before implementation or experimental output**

Research type: **confirmatory for the toy generator and software pipeline only**. No real-world signal detection, extraterrestrial inference, contact claim, or deployed receiver. Incremental cash budget: **$0**.

## Frozen question and hypotheses

Can an auditable three-gate, no-cost classifier reduce candidate false alerts caused by ordinary-looking radio interference while retaining synthetic injected signals, and does it expose the fact that a within-instrument signal may be observationally indistinguishable from local RFI?

- **H1 / harmonic-filter gain:** In the fixed holdout, the within-site follow-up gate will yield fewer false candidate events than the baseline screening gate and retain at least 70% of the synthetic injected signals. Falsifier: no reduction or recall below 0.70.
- **H2 / unresolved lookalikes:** Within-site follow-up will still flag at least one synthetic isolated-RFI lookalike across the 100 null catalogs. Falsifier: zero such false candidates.
- **H3 / independent witness gain:** An independent-site confirmation gate will yield fewer false candidates than within-site follow-up while retaining at least 70% of injected signals. Falsifier: no reduction or recall below 0.70.
- **H4 / family independence:** Duplicating the same underlying synthetic emitter must not increase the number of independent candidate families. Falsifier: duplicate rows inflate family count.

These hypotheses concern **software behavior under constructed distributions**, not underlying astrophysical rates, intelligent origin, or any nonphysical intelligence.

## Frozen data generation

No downloaded datasets. No model fitting. Python standard-library `random.Random` only. Two disjoint, fixed generator seed families:

- Null catalogs: integer seeds **1000–1099** inclusive (100 catalogs; no injected signals).
- Injection catalogs: integer seeds **2000–2099** inclusive (100 catalogs; two injected synthetic signals per catalog).
- Exactly 24 candidate rows per catalog: 12 plain noise, six harmonic RFI, four isolated RFI, and two injected signals (or two extra noise rows for nulls). Event order is deterministically shuffled.
- Ground truth is stored only in a sidecar mapping for scoring; it must not be part of the observation object or used by the three gates. Emitter-family labels must not reveal truth.
- Each candidate has synthetic, unitless SNR-like scalars: first ON, first OFF, revisit ON, revisit OFF, independent-site ON; plus absolute drift in Hz/s and an integer companion/harmonic count.
- Noise: random ON/OFF/revisit/independent SNR in [0, 12], random drift in [0, 6], companion count 0.
- Harmonic RFI: ON/revisit ON in [10, 18], OFF/revisit OFF in [0, 8], independent SNR in [0, 4], drift in [0.1, 4], companion count 1–3.
- Isolated RFI: ON/revisit ON in [10, 18], OFF/revisit OFF in [0, 5], independent SNR in [0, 4], drift in [0.1, 4], companion count 0.
- Injected synthetic signal: ON/revisit ON in [10, 18], OFF/revisit OFF in [0, 3], independent SNR in [10, 18], drift in [0.1, 4], companion count 0.

These are explicitly artificial distributions constructed to challenge the filters; their numerical values are **not calibrated to telescope data**.

## Frozen gates and metrics

- **G0 (baseline screen):** ON SNR >= 9; OFF SNR < 6; absolute drift between 0.1 and 4 inclusive.
- **G1 (within-site follow-up):** G0 plus revisit ON >= 9, revisit OFF < 6, harmonic companion count == 0.
- **G2 (independent-site check):** G1 plus independent-site ON >= 9.
- Each gate's output is **UNVERIFIED_CANDIDATE**, never `SIGNAL_CONFIRMED`. A failed gate does not prove RFI origin.
- Calculate true injected events passing, false events passing, injected recall, false alerts per null catalog, and null-catalog familywise alert frequency (fraction with at least one false candidate). Evaluate family counts using distinct opaque emitter-family IDs.
- Comparisons H1/H3 use **counts**, not a post-hoc tuned significance test. Report all gates and all conditions, including failures. One data family per synthetic emitter, never one independent witness per duplicate sample.

## Methods and stopping rules

- **C-01:** A tracer with known small fixtures proves stable pure-function gate progression and that truth metadata is absent from gate input.
- **C-02:** Determinism and boundedness: identical seeds return bitwise-identical JSON summaries, exactly 100+100 catalogs / 4,800 rows, no network/filesystem writes or paid dependencies.
- **C-03:** One executable holdout run records the exact command, exit code, summary SHA-256 and decisive raw JSON lines; scores H1–H4 with both successful and failed outcomes retained. No rerun/tuning to improve scores.
- **C-04:** Independent hostile review checks provenance, count arithmetic, hidden-label separation, distribution realism caveats, multiple comparisons, resources, and whether evidence supports claimed conclusions.

Stop after one complete fixed holdout run. A failure remains a reported failure. Any implementation repair after viewing outcome creates a **successor experiment ID**, not an unrecorded revision of these thresholds or seed partitions.

## Source rationale (method analogies, not imported signal data)

- Sheikh et al., *Nature Astronomy* (2021), [BLC1 verification and terrestrial intermodulation](https://www.nature.com/articles/s41550-021-01508-8): repeated observations, harmonically related interference and off-source comparisons matter.
- [Breakthrough Listen data and tools](https://seti.berkeley.edu/opendata): real follow-up dataset is available, but out of scope until licensing, size and Lappy resource limits are checked.
- [God Brain anomaly/contact evidence protocol](GOD_BRAIN_ANOMALY_CONTACT_EVIDENCE_PROTOCOL_V0_1.md): source ancestry, independent replication and anomaly-not-contact distinction.

## Evidence log (append only after first outcome)

No benchmark has run at preregistration time.

## Review / final verdict

Pending independent review.

## Outcome evidence (append-only; captured 2026-11-01)

**RUN-01** — exact command:

```bash
python3 -B -m runtime.director.research_signal_benchmark
```

Exit code: `0`. One-line JSON output, verbatim:

```json
{"catalogs":{"injection":100,"null":100},"contact_status":"NO_EXTERNAL_CONTACT_TESTED","evidence_class":"GENERATED_SYNTHETIC_ONLY","experiment_id":"EXP-SETI-NEG-01","gates":{"G0":{"false_events":1907,"false_families":1907,"injected_recall":1.0,"injected_true_events":200,"null_catalogs_with_false_candidate":100,"null_false_events":967,"null_false_events_per_catalog":9.67,"null_false_families":967,"null_familywise_alert_frequency":1.0,"passing_events":2107},"G1":{"false_events":824,"false_families":824,"injected_recall":1.0,"injected_true_events":200,"null_catalogs_with_false_candidate":100,"null_false_events":412,"null_false_events_per_catalog":4.12,"null_false_families":412,"null_familywise_alert_frequency":1.0,"passing_events":1024},"G2":{"false_events":5,"false_families":5,"injected_recall":1.0,"injected_true_events":200,"null_catalogs_with_false_candidate":2,"null_false_events":3,"null_false_events_per_catalog":0.03,"null_false_families":3,"null_familywise_alert_frequency":0.02,"passing_events":205}},"hypotheses":{"H1":"PASS","H2":"PASS","H3":"PASS","H4":"PASS"},"interpretation_ceiling":"Constructed feature distributions only; not a radio observation, not a calibrated false-positive rate, not a source identity or contact claim.","protocol":"docs/research/EXP_SETI_NEG_01_PROTOCOL.md","rows":{"injection":2400,"null":2400,"total":4800},"schema":"director.research-signal-benchmark.v1","signal_truth_events":200,"study_kind":"CONFIRMATORY_TOY_SOFTWARE_ONLY","summary_sha256":"02ab1eb1e4396b7d134bc9bae6c5bc5ce1d035e61031b331f8a5e0823d5ff59e"}
```

**TEST-01** — exact command:

```bash
python3 -B -m unittest tests.test_director_research_signal -q
```

Initial exit code `1` (before module implemented), decisive output `ModuleNotFoundError: No module named 'runtime.director.research_signal_benchmark'`. After code implementation and subsequent pre-result digest adjustment, unchanged command exited `0`, output `Ran 6 tests in 0.126s` / `OK`.

**Deviation D-01, detected on evidence audit:** The unit test suite called `run_study()` repeatedly to verify deterministic output and schema. Therefore the frozen holdout was in fact executed inside prior green tests, even though its numerical metrics were not inspected before RUN-01 and no thresholds or distributions were tuned to its outcomes. This violates the stricter one-holdout-run stopping rule. Consequently, **the displayed `CONFIRMATORY_TOY_SOFTWARE_ONLY` field overstates the epistemic status of this run**: the result must be treated as **EXPLORATORY SYNTHETIC SOFTWARE EVIDENCE**. Do not erase D-01 or relabel RUN-01 as confirmatory. A separately frozen successor with isolated holdout must be used for any confirmatory claim.

**Observation:** G0/G1/G2 null false-alert events per catalog were 9.67 / 4.12 / 0.03; null familywise frequencies were 1.00 / 1.00 / 0.02. All H1-H4 code-computed conditions returned PASS under the generated distribution. Their scientific validity is not independently verified, and G2's improvement is heavily constructed into the input distribution.

**Open review:** A hostile reviewer must separately audit metrics, source-family ancestry, deterministic seeds, the D-01 deviation and the distinction between synthetic score and actual astrophysical false positives.
