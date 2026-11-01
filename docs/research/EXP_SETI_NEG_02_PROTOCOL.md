# EXP-SETI-NEG-02 — overlap, common causes and source-family successor

Status: **FROZEN SUCCESSOR DESIGN — 2026-11-01, before V02 results**.

This is a replacement of the *engineering evaluation*, not a retroactive correction of EXP-SETI-NEG-01 results. Its classification is **EXPLORATORY SYNTHETIC SOFTWARE ONLY**, even if the code-level predictions pass. Neither output is observational evidence of contact, a verified radio technosignature, or a measured telescope false-positive rate.

## Origin and original immutable outcome

EXP-SETI-NEG-01 was independently rejected as a *completed confirmatory* study because its test suite repeatedly executed the holdout. Its original source had SHA-256 `cb302f20ec6f7ae9b9ee4eaf11c02eb27a56ea043d66a818d0b3908ed6d05a4c`; full original result and `summary_sha256=02ab1eb1e4396b7d134bc9bae6c5bc5ce1d035e61031b331f8a5e0823d5ff59e` remain in [EXP-SETI-NEG-01](EXP_SETI_NEG_01_PROTOCOL.md). The original program output inaccurately marked this result confirmatory. Do not erase or reclassify that original JSON; this successor corrects the source-level claim.

## Frozen question, predictions, falsifiers

Can the same three fixed gates remain interpretable when signal and RFI site features overlap, repeat visits are correlated by emitter, and an independent site may itself see RFI or miss a synthetic injected source?

- **H1:** G1 reduces false-event candidates relative to G0 and retains >=70% injected-signal recall. Falsifier: no false reduction or recall below threshold.
- **H2:** G1 leaves nonzero explicitly **isolated-RFI** false candidates under null catalogs. Falsifier: none survive.
- **H3:** G2 reduces G1 false events and retains >=70% injected signals, while leaving **some** null false candidates (no perfect artificial separation). Falsifier: no reduction, poor recall, or zero null false candidates.
- **H4:** Repeated harmonic/isolated source rows have identical family IDs within a catalog; at G1 the number of independent null false families is strictly less than the number of null false events. Falsifier: no actual family duplicates or no reduction.

These are deterministic software assertions under artificial distributions. A PASS is not a discovery, physics evidence, or independent external replication.

## Frozen generation and gates

- Standard library `random.Random`, 24 rows/catalog, no network, file, or paid model.
- **Holdout:** null seed 3000–3099 inclusive; injection 4000–4099 inclusive (100 each).
- **Development fixtures:** null seeds 1–99 and injection seeds 101–199; tests must **not call `run_study()` before the first recorded holdout command**. Development fixture metrics do not count as confirmatory data.
- Each catalog: 12 noise rows, 6 harmonic RFI rows in **3 source families**, 4 isolated RFI rows in **2 source families**, and 2 additional noise (null) or 2 injected signal rows (injection).
- Event/family identifiers are generated as 64-bit pseudorandom hex labels without embedding seed or kind in their visible syntax. This is obfuscation, not cryptographic hiding of a known deterministic experiment; the gate must never inspect IDs for evidence.
- Noise first-site/on/off/revisit/independent SNR in [0,12], drift [0,6], companions 0.
- Harmonic RFI on/revisit ON [10,18], OFF/revisit OFF [0,8], **independent site [0,12]**, drift [0.1,4], companions 1–3.
- Isolated RFI on/revisit ON [10,18], OFF/revisit OFF [0,5], drift [0.1,4], companions 0. **Exactly index 0 of the four** isolated rows per catalog models a common-mode interferer with independent-site SNR [10,18]; the other three use independent-site SNR [0,12]. This may cause false candidates even after G2.
- Injected synthetic signals on/revisit ON [10,18], OFF/revisit OFF [0,3], drift [0.1,4], companions 0; independently generated site [7,18], except when `(seed * 2 + signal_index) % 10 == 0`, when independent-site failure is [0,5]. Signal indexes are 0 and 1 before final event shuffle, giving 10% failures across the 100 holdout catalogs. These ranges overlap the RFI class, so an independent-site threshold cannot perfectly identify origin.
- Gate thresholds unchanged from first experiment: **G0** ON>=9, OFF<6, drift 0.1..4; **G1** G0 + revisit ON>=9, revisit OFF<6, companions==0; **G2** G1 + independent-site ON>=9.
- Every passing gate marks only `UNVERIFIED_CANDIDATE`. Report per-gate total false events, false families, by-source-class false alerts (scorer-only), injected recall, null events/catalog and familywise null rate.
- **No fitting, tuning, validation against astronomy, or changes to these thresholds** after seeing holdout results.

## Fixed method and stop conditions

1. Unit tests prove a three-gate tracer, field/label isolation, duplicate-family accounting, input rejection, development-seed determinism, and output schema shape without running the holdout.
2. Run the exact CLI **once**: `python3 -B -m runtime.director.research_signal_benchmark`. Save exit code, verbatim JSON, and canonical summary SHA-256. No rerun to optimize numbers.
3. Independently review code, arithmetic and evidence; a reviewer may replay source to check determinism but must not treat that as new independent scientific data.
4. Stop after one recorded holdout, regardless of H1–H4 result. Any subsequently altered generator/threshold is another successor experiment ID.

Resource bound: exactly 200 x 24 = 4800 tiny rows; no Lappy host work, external data, subscriptions or deployment. This is a source-level synthetic benchmark; real Breakthrough Listen slices need their own dataset, storage and license qualification.

Primary methodological analogue: [BLC1 interference verification](https://www.nature.com/articles/s41550-021-01508-8). Hypothesis/result provenance must retain its ordinary explanations under the God Brain [anomaly protocol](GOD_BRAIN_ANOMALY_CONTACT_EVIDENCE_PROTOCOL_V0_1.md).

## Append-only evidence log

No successor holdout result exists at preregistration.

## Independent review

Pending.

## Outcome evidence (append-only; recorded 2026-11-01)

**RUN-02** — executed the frozen holdout **once**, after six development-only tests had passed.

Exact command: `python3 -B -m runtime.director.research_signal_benchmark`

Exit code: `0`. Exact stdout (one JSON line):

```json
{"catalogs":{"injection":100,"null":100},"contact_status":"NO_EXTERNAL_CONTACT_TESTED","evidence_class":"GENERATED_SYNTHETIC_ONLY","experiment_id":"EXP-SETI-NEG-02","gates":{"G0":{"false_events":1876,"false_families":1161,"injected_recall":1.0,"injected_true_events":200,"null_catalogs_with_false_candidate":100,"null_false_by_origin":{"HARMONIC_RFI":430,"ISOLATED_RFI":400,"NOISE":106},"null_false_events":936,"null_false_events_per_catalog":9.36,"null_false_families":584,"null_familywise_alert_frequency":1.0,"passing_events":2076},"G1":{"false_events":826,"false_families":426,"injected_recall":1.0,"injected_true_events":200,"null_catalogs_with_false_candidate":100,"null_false_by_origin":{"HARMONIC_RFI":0,"ISOLATED_RFI":400,"NOISE":14},"null_false_events":414,"null_false_events_per_catalog":4.14,"null_false_families":214,"null_familywise_alert_frequency":1.0,"passing_events":1026},"G2":{"false_events":369,"false_families":298,"injected_recall":0.705,"injected_true_events":141,"null_catalogs_with_false_candidate":100,"null_false_by_origin":{"HARMONIC_RFI":0,"ISOLATED_RFI":181,"NOISE":4},"null_false_events":185,"null_false_events_per_catalog":1.85,"null_false_families":149,"null_familywise_alert_frequency":1.0,"passing_events":510}},"hypotheses":{"H1":"PASS","H2":"PASS","H3":"PASS","H4":"PASS"},"interpretation_ceiling":"Constructed feature distributions only; not a radio observation, not a calibrated false-positive rate, not a source identity or contact claim.","protocol":"docs/research/EXP_SETI_NEG_02_PROTOCOL.md","rows":{"injection":2400,"null":2400,"total":4800},"schema":"director.research-signal-benchmark.v1","signal_truth_events":200,"study_kind":"EXPLORATORY_SYNTHETIC_SOFTWARE_ONLY","summary_sha256":"ef87d74f237092f2e35f3dbe1ceb7cbe075b0bd0ea7019551eb53e3898cae39b"}
```

**Observation O-02:** All four predeclared software predicates returned PASS for these *constructed* values. G2 had 1.85 null false candidates/catalog and a null familywise alert frequency of **1.0**, so every null catalog still had at least one misleading candidate. G2 also rejected 59 of 200 synthetic injected signals (recall **0.705**) after independent-site failure and overlapping distributions were introduced. This is substantially less optimistic than EXP-SETI-NEG-01 and cautions against interpreting a candidate alert as an identified message.

**No deviations in RUN-02 observed by the author:** The six pre-run tests used development seeds only, not `run_study()`; the frozen holdout CLI was called once. Independent verification and a full-suite source check remain pending.

## Independent hostile verification (2026-11-01; append-only)

**Verdict: `confirmed` for source-scoped synthetic outputs and test behavior; `inconclusive` for externally proving one-shot/freeze chronology.** No P0/P1 scientific/source findings; two P2 limitations below.

- Reviewer independently recalculated all 200 catalogs (4,800 events), the three gates, every null class breakdown and unique source-family tally **without** calling the implementation's `_score_catalog` or `classify_gates`. Matched G0 `2076 total / 936 null false / 584 null false families`, G1 `1026 / 414 / 214`, G2 `510 / 185 / 149`. Recomputed full canonical SHA-256 `ef87d74f237092f2e35f3dbe1ceb7cbe075b0bd0ea7019551eb53e3898cae39b`.
- Exact reviewer check `python3 -B -m unittest tests.test_director_research_signal -q`: exit `0`, 6 tests passed. Author's full root suite check `python3 -B -m unittest discover -s tests -q`: exit `0`, `Ran 577 tests in 9.457s`, `OK (skipped=3)`. Both are **software** verification, not telescope/host qualification.
- **C-01 confirmed**, test tracer and truth-field separation. **C-02 confirmed** for fixed source counts, boundedness, unique events and grouped RFI families. **C-03 output/metrics confirmed**, while the chronology of one-only execution and freeze-before-results remains author-logged, **not externally attested**. **C-04 confirmed**, independent hostile source review occurred.
- **P2 realism caveat:** The two rows assigned to each RFI source family have the same **lineage ID**, but their synthetic feature vectors are drawn independently. The model enforces family bookkeeping, **not correlated waveform physics**, and the independent-site scalar is a generated number, not a second observatory.
- **P2 custody caveat:** The experiment's append-only Markdown is not tamper-resistant custody. The program therefore correctly labels itself **exploratory synthetic software** regardless of the recorded one-run procedure.

**Closeout:** this synthetic experiment completed at its source/metrics level. All H1–H4 happen to pass in the constructed generator; no claim about external intelligence, real false-alarm prevalence, independent physical instrumentation, or subjective consciousness follows. Future claims require real-data provenance, calibration, genuine independent instruments and frozen holdouts with independently enforceable custody.
