"""Preregistered synthetic BLC1-inspired negative-control benchmark.

Tests are for deterministic software behavior, not physical signals or ETI.
"""

import dataclasses
import unittest

from runtime.director.research_signal_benchmark import (
    EXPERIMENT_ID,
    ObservedCandidate,
    STUDY_KIND,
    classify_gates,
    count_unique_families,
    generate_catalog,
)


def _candidate(**overrides):
    values = dict(
        event_id="evt-1", family_id="opaque-family",
        on_snr=12.0, off_snr=1.0,
        revisit_on_snr=11.0, revisit_off_snr=2.0,
        independent_site_on_snr=12.0,
        abs_drift_hz_per_s=1.0, harmonic_companions=0,
    )
    values.update(overrides)
    return ObservedCandidate(**values)


class SignalBenchmarkTests(unittest.TestCase):
    def test_tracer_three_gate_progression(self):
        self.assertEqual(classify_gates(_candidate()), (True, True, True))
        self.assertEqual(
            classify_gates(_candidate(harmonic_companions=2)),
            (True, False, False),
        )
        self.assertEqual(
            classify_gates(_candidate(independent_site_on_snr=2.0)),
            (True, True, False),
        )
        self.assertEqual(
            classify_gates(_candidate(on_snr=7.0)),
            (False, False, False),
        )

    def test_ground_truth_not_in_candidate_features(self):
        names = {field.name for field in dataclasses.fields(ObservedCandidate)}
        self.assertFalse(names.intersection({"kind", "truth", "origin", "label"}))
        events, truth = generate_catalog(1, inject=False)
        self.assertEqual(len(events), 24)
        self.assertEqual(len(truth), 24)
        self.assertEqual(set(truth), {event.event_id for event in events})
        self.assertTrue(all("kind" not in dataclasses.asdict(e) for e in events))
        self.assertEqual(sum(kind == "INJECTED" for kind in truth.values()), 0)
        self.assertEqual(sum(kind == "INJECTED" for kind in generate_catalog(101, inject=True)[1].values()), 2)

    def test_deterministic_development_catalogs_not_holdout(self):
        self.assertEqual(generate_catalog(33, inject=False), generate_catalog(33, inject=False))
        self.assertNotEqual(generate_catalog(33, inject=False), generate_catalog(34, inject=False))
        self.assertEqual(len(generate_catalog(133, inject=True)[0]), 24)
        for seed, injected in ((33, False), (133, True)):
            events, _ = generate_catalog(seed, inject=injected)
            self.assertLess(count_unique_families(events), len(events))
            self.assertTrue(all(item.event_id.startswith("evt-") and len(item.event_id) == 20 for item in events))

    def test_duplicates_are_one_source_family(self):
        events = [_candidate(event_id="a", family_id="f-1"),
                  _candidate(event_id="b", family_id="f-1"),
                  _candidate(event_id="c", family_id="f-2")]
        self.assertEqual(count_unique_families(events), 2)

    def test_nonfinite_and_out_of_range_refused(self):
        for kwargs in ({"on_snr": float("nan")},
                       {"off_snr": float("inf")},
                       {"harmonic_companions": -1},
                       {"family_id": ""}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                _candidate(**kwargs)

    def test_successor_metadata_and_overlap_without_holdout(self):
        self.assertEqual(EXPERIMENT_ID, "EXP-SETI-NEG-02")
        self.assertEqual(STUDY_KIND, "EXPLORATORY_SYNTHETIC_SOFTWARE_ONLY")
        observations, truth = generate_catalog(133, inject=True)
        isolated = [c for c in observations if truth[c.event_id] == "ISOLATED_RFI"]
        self.assertEqual(len(isolated), 4)
        self.assertTrue(any(c.independent_site_on_snr >= 9 for c in isolated))
        self.assertEqual(len({c.family_id for c in isolated}), 2)
        self.assertTrue(all(c.family_id.startswith("fam-") and len(c.family_id) == 20 for c in isolated))
        self.assertEqual(len({c.family_id for c in observations}), 19)


if __name__ == "__main__":
    unittest.main()
