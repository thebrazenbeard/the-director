"""EXP-SETI-NEG-02: finite synthetic candidate / interference benchmark.

Feature-level *simulation*, not radio acquisition, a technosignature detector,
or evidence about an outside-reality entity. This module uses only stdlib and
never performs network, host, or filesystem effects at import or in run_study.
The one-shot CLI emits a bounded JSON receipt to standard output.

The frozen successor protocol is docs/research/EXP_SETI_NEG_02_PROTOCOL.md.
The rejected confirmatory claim from EXP-SETI-NEG-01 is preserved in its
original evidence log; this code explicitly labels its results exploratory.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import random
from collections import defaultdict
from typing import Mapping, Sequence


GATES = ("G0", "G1", "G2")
NULL_SEEDS = range(3000, 3100)
INJECTION_SEEDS = range(4000, 4100)
DEV_NULL_SEEDS = range(1, 100)
DEV_INJECTION_SEEDS = range(101, 200)
ROWS_PER_CATALOG = 24
EXPERIMENT_ID = "EXP-SETI-NEG-02"
STUDY_KIND = "EXPLORATORY_SYNTHETIC_SOFTWARE_ONLY"


@dataclasses.dataclass(frozen=True)
class ObservedCandidate:
    """Observable features only; source labels are kept in a separate sidecar."""

    event_id: str
    family_id: str
    on_snr: float
    off_snr: float
    revisit_on_snr: float
    revisit_off_snr: float
    independent_site_on_snr: float
    abs_drift_hz_per_s: float
    harmonic_companions: int

    def __post_init__(self) -> None:
        for name in ("event_id", "family_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not 1 <= len(value) <= 64:
                raise ValueError(f"{name} must be bounded nonempty text")
            if not all(c.isascii() and (c.isalnum() or c in "-_.") for c in value):
                raise ValueError(f"{name} must be a bounded ASCII identifier")
        for name in (
            "on_snr", "off_snr", "revisit_on_snr", "revisit_off_snr",
            "independent_site_on_snr", "abs_drift_hz_per_s",
        ):
            number = getattr(self, name)
            if type(number) not in (int, float):
                raise ValueError(f"{name} must be numeric")
            try:
                finite = math.isfinite(number)
            except (OverflowError, ValueError):
                finite = False
            if not finite or number < 0 or number > 100:
                raise ValueError(f"{name} must be bounded and finite")
        if type(self.harmonic_companions) is not int or not 0 <= self.harmonic_companions <= 10:
            raise ValueError("harmonic_companions must be integer 0..10")


def classify_gates(item: ObservedCandidate) -> tuple[bool, bool, bool]:
    """Gate status for an *unverified* candidate; never identifies a sender."""
    if not isinstance(item, ObservedCandidate):
        raise ValueError("expected observed candidate only")
    baseline = (
        item.on_snr >= 9
        and item.off_snr < 6
        and 0.1 <= item.abs_drift_hz_per_s <= 4
    )
    followup = (
        baseline
        and item.revisit_on_snr >= 9
        and item.revisit_off_snr < 6
        and item.harmonic_companions == 0
    )
    independent = followup and item.independent_site_on_snr >= 9
    return baseline, followup, independent


def count_unique_families(items: Sequence[ObservedCandidate]) -> int:
    """Derived copies of one emitter are one source family, not witnesses."""
    if len(items) > 10000 or any(not isinstance(item, ObservedCandidate) for item in items):
        raise ValueError("candidate families must be bounded observations")
    return len({item.family_id for item in items})


def _features(rng: random.Random, kind: str, *, common: bool = False,
              failure: bool = False) -> tuple[float, ...]:
    """Only the generator knows ground-truth kind; output has no label."""
    if kind == "NOISE":
        return (
            rng.uniform(0, 12), rng.uniform(0, 12),
            rng.uniform(0, 12), rng.uniform(0, 12),
            rng.uniform(0, 12), rng.uniform(0, 6), 0,
        )
    if kind == "HARMONIC_RFI":
        return (
            rng.uniform(10, 18), rng.uniform(0, 8),
            rng.uniform(10, 18), rng.uniform(0, 8),
            rng.uniform(0, 12), rng.uniform(0.1, 4), rng.randint(1, 3),
        )
    if kind == "ISOLATED_RFI":
        return (
            rng.uniform(10, 18), rng.uniform(0, 5),
            rng.uniform(10, 18), rng.uniform(0, 5),
            rng.uniform(10, 18) if common else rng.uniform(0, 12),
            rng.uniform(0.1, 4), 0,
        )
    if kind == "INJECTED":
        return (
            rng.uniform(10, 18), rng.uniform(0, 3),
            rng.uniform(10, 18), rng.uniform(0, 3),
            rng.uniform(0, 5) if failure else rng.uniform(7, 18),
            rng.uniform(0.1, 4), 0,
        )
    raise ValueError("unregistered synthetic source kind")


def generate_catalog(seed: int, *, inject: bool) -> tuple[tuple[ObservedCandidate, ...], dict[str, str]]:
    """Construct the frozen 24-row catalog with a separate label sidecar."""
    if type(inject) is not bool:
        raise ValueError("inject must be bool")
    valid_ranges = ((INJECTION_SEEDS, DEV_INJECTION_SEEDS) if inject
                    else (NULL_SEEDS, DEV_NULL_SEEDS))
    if type(seed) is not int or not any(seed in group for group in valid_ranges):
        raise ValueError("seed is not in an allowed development/holdout partition")
    rng = random.Random(seed)
    # Group IDs are internal data lineage, never truth labels exposed to gate
    # logic. Two records from each RFI family represent correlated origin.
    rows: list[tuple[str, int, tuple[float, ...]]] = []
    for index in range(12 if inject else 14):
        rows.append(("NOISE", index, _features(rng, "NOISE")))
    for index in range(6):
        rows.append(("HARMONIC_RFI", index // 2, _features(rng, "HARMONIC_RFI")))
    for index in range(4):
        rows.append(("ISOLATED_RFI", index // 2,
                     _features(rng, "ISOLATED_RFI", common=(index == 0))))
    if inject:
        for index in range(2):
            failure = (seed * 2 + index) % 10 == 0
            rows.append(("INJECTED", index,
                         _features(rng, "INJECTED", failure=failure)))
    if len(rows) != ROWS_PER_CATALOG:
        raise ValueError("frozen catalog is not 24 events")
    rng.shuffle(rows)
    observations: list[ObservedCandidate] = []
    truth: dict[str, str] = {}
    family_ids: dict[tuple[str, int], str] = {}
    for kind, lineage, feature in rows:
        key = (kind, lineage)
        if key not in family_ids:
            family_ids[key] = f"fam-{rng.getrandbits(64):016x}"
        family = family_ids[key]
        event_id = f"evt-{rng.getrandbits(64):016x}"
        if event_id in truth:
            raise ValueError("synthetic event ID collision")
        observations.append(ObservedCandidate(event_id, family, *feature))
        truth[event_id] = kind
    return tuple(observations), truth


def _score_catalog(
    observations: Sequence[ObservedCandidate],
    truth: Mapping[str, str],
    *,
    inject: bool,
    buckets: dict[str, dict[str, object]],
) -> None:
    if len(observations) != ROWS_PER_CATALOG or set(truth) != {c.event_id for c in observations}:
        raise ValueError("catalog identity or length mismatch")
    for gate in GATES:
        bucket = buckets[gate]
        idx = GATES.index(gate)
        selected = [c for c in observations if classify_gates(c)[idx]]
        true = [c for c in selected if truth[c.event_id] == "INJECTED"]
        false = [c for c in selected if truth[c.event_id] != "INJECTED"]
        bucket["passing_events"] += len(selected)
        bucket["injected_true_events"] += len(true)
        bucket["false_events"] += len(false)
        bucket["false_families"] += count_unique_families(false)
        if not inject:
            bucket["null_false_events"] += len(false)
            bucket["null_false_families"] += count_unique_families(false)
            bucket["null_catalogs_with_false_candidate"] += bool(false)
            for item in false:
                key = f"null_false_{truth[item.event_id].lower()}"
                bucket[key] += 1


def run_study() -> dict[str, object]:
    """Compute the one fixed evaluation; no fitting or external acquisition."""
    buckets: dict[str, dict[str, object]] = {
        gate: defaultdict(int) for gate in GATES
    }
    for inject, seeds in ((False, NULL_SEEDS), (True, INJECTION_SEEDS)):
        for seed in seeds:
            observations, truth = generate_catalog(seed, inject=inject)
            _score_catalog(observations, truth, inject=inject, buckets=buckets)

    reported: dict[str, dict[str, object]] = {}
    for gate in GATES:
        data = buckets[gate]
        reported[gate] = {
            "passing_events": data["passing_events"],
            "injected_true_events": data["injected_true_events"],
            "false_events": data["false_events"],
            "false_families": data["false_families"],
            "null_false_events": data["null_false_events"],
            "null_false_families": data["null_false_families"],
            "null_catalogs_with_false_candidate": data["null_catalogs_with_false_candidate"],
            "null_familywise_alert_frequency": data["null_catalogs_with_false_candidate"] / len(NULL_SEEDS),
            "null_false_events_per_catalog": data["null_false_events"] / len(NULL_SEEDS),
            "injected_recall": data["injected_true_events"] / (2 * len(INJECTION_SEEDS)),
            "null_false_by_origin": {
                kind: data[f"null_false_{kind.lower()}"]
                for kind in ("NOISE", "HARMONIC_RFI", "ISOLATED_RFI")
            },
        }
    duplicate = [
        ObservedCandidate("a", "same", 10, 2, 10, 2, 10, 1, 0),
        ObservedCandidate("b", "same", 10, 2, 10, 2, 10, 1, 0),
    ]
    hypotheses = {
        "H1": reported["G1"]["false_events"] < reported["G0"]["false_events"]
              and reported["G1"]["injected_recall"] >= 0.70,
        "H2": reported["G1"]["null_false_by_origin"]["ISOLATED_RFI"] >= 1,
        "H3": reported["G2"]["false_events"] < reported["G1"]["false_events"]
              and reported["G2"]["injected_recall"] >= 0.70
              and reported["G2"]["null_false_events"] >= 1,
        "H4": (count_unique_families(duplicate) == 1
               and reported["G1"]["null_false_families"] < reported["G1"]["null_false_events"]),
    }
    payload = {
        "schema": "director.research-signal-benchmark.v1",
        "experiment_id": EXPERIMENT_ID,
        "study_kind": STUDY_KIND,
        "evidence_class": "GENERATED_SYNTHETIC_ONLY",
        "contact_status": "NO_EXTERNAL_CONTACT_TESTED",
        "protocol": "docs/research/EXP_SETI_NEG_02_PROTOCOL.md",
        "catalogs": {"null": len(NULL_SEEDS), "injection": len(INJECTION_SEEDS)},
        "rows": {"null": len(NULL_SEEDS) * ROWS_PER_CATALOG,
                 "injection": len(INJECTION_SEEDS) * ROWS_PER_CATALOG,
                 "total": (len(NULL_SEEDS) + len(INJECTION_SEEDS)) * ROWS_PER_CATALOG},
        "signal_truth_events": 2 * len(INJECTION_SEEDS),
        "gates": reported,
        "hypotheses": {key: "PASS" if value else "FAIL" for key, value in hypotheses.items()},
        "interpretation_ceiling": (
            "Constructed feature distributions only; not a radio observation, "
            "not a calibrated false-positive rate, not a source identity or contact claim."
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {**payload, "summary_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}


def main() -> None:
    print(json.dumps(run_study(), sort_keys=True, separators=(",", ":"), allow_nan=False))


if __name__ == "__main__":
    main()
