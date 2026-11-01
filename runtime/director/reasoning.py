"""Bounded, evidence-grounded local reasoning for The Director.

This is an actual (but deliberately narrow) symbolic reasoner: it checks
conditions against a changing world model, compares hypothetical outcomes,
identifies evidence gaps, and explains why an option is or is not supported.
It requires no hosted model, generates no fabricated observations, performs no
effects, and must not be advertised as general intelligence.  Caller-provided
observations, provenance labels, and assumptions remain unauthenticated.

This module has no network, process, model-provider, or filesystem interface.
"""

from __future__ import annotations

import math
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any


_MAX_TEXT = 2048
_MAX_FACTS = 512
_MAX_OPTIONS = 64
_MAX_PRECONDITIONS = 16
_MAX_HISTORY = 64
_MAX_ABS = 1_000_000_000
_MAX_WORLD_ABS = 1_000_000_000_000
_MAX_WORLD_TEXT = 512
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")


def _text(value: object, name: str, *, allow_empty: bool = False, multiline: bool = False) -> str:
    if not isinstance(value, str) or len(value) > _MAX_TEXT or (not allow_empty and not value.strip()):
        raise ValueError(f"{name} must be bounded nonempty text")
    if not (all(c.isprintable() or (multiline and c in "\r\n\t") for c in value)):
        raise ValueError(f"{name} must not contain control characters")
    return value


def _id(value: object, name: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{name} must be a bounded identifier")
    return value


def _number(value: object, name: str, *, limit: int = _MAX_ABS) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be a number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be finite and bounded") from exc
    if not math.isfinite(number) or abs(number) > limit:
        raise ValueError(f"{name} must be finite and bounded")
    return number


def _fmt(value: float) -> str:
    return format(value, ".8g")


def _goal(raw: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        raise ValueError("goal must be a mapping")
    required = {"goal_id", "objective", "metric", "target_value", "direction"}
    if set(raw) != required:
        raise ValueError("goal must contain the Director goal fields")
    direction = raw["direction"]
    if direction not in ("INCREASE", "DECREASE"):
        raise ValueError("goal direction must be INCREASE or DECREASE")
    return {"goal_id": _id(raw["goal_id"], "goal_id"),
            "objective": _text(raw["objective"], "objective"),
            "metric": _id(raw["metric"], "metric"),
            "target_value": _number(raw["target_value"], "target_value"),
            "direction": direction}


def _world(raw: Mapping[str, object]) -> tuple[int, dict[str, dict[str, object]]]:
    if not isinstance(raw, Mapping):
        raise ValueError("world_snapshot must be a mapping")
    revision = raw.get("revision")
    if type(revision) is not int or not 0 <= revision <= 2**31 - 1:
        raise ValueError("world_snapshot revision must be nonnegative integer")
    facts = raw.get("facts")
    if not isinstance(facts, Mapping) or len(facts) > _MAX_FACTS:
        raise ValueError("world_snapshot facts must be bounded mapping")
    normalized: dict[str, dict[str, object]] = {}
    for key, value in facts.items():
        ident = _id(key, "fact identifier")
        if not isinstance(value, Mapping):
            raise ValueError("fact must be mapping")
        status = value.get("status")
        if status not in ("OBSERVED", "CONTESTED", "INFERRED", "HYPOTHETICAL", "UNKNOWN"):
            raise ValueError("fact status invalid")
        observed_value = None
        if status == "OBSERVED":
            scalar = value.get("value")
            if type(scalar) in (int, float):
                observed_value = _number(scalar, "observed fact value", limit=_MAX_WORLD_ABS)
            elif type(scalar) is bool:
                observed_value = scalar
            elif isinstance(scalar, str) and len(scalar) <= _MAX_WORLD_TEXT:
                observed_value = scalar
            else:
                raise ValueError("observed fact must be a bounded scalar")
        evidence = value.get("evidence_ids", ())
        if not isinstance(evidence, (list, tuple)) or len(evidence) > 1024:
            raise ValueError("evidence_ids must be a bounded list")
        normalized[ident] = {"status": status, "value": observed_value,
                             "evidence_ids": sorted(_id(e, "evidence_id") for e in evidence)}
    return revision, normalized


def _preconditions(raw: object) -> list[dict[str, object]]:
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)) or len(raw) > _MAX_PRECONDITIONS:
        raise ValueError("preconditions must be bounded list")
    result = []
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != {"metric", "operator", "value"}:
            raise ValueError("precondition fields must be metric, operator, value")
        op = item["operator"]
        if op not in ("EQ", "GE", "LE"):
            raise ValueError("unsupported precondition operator")
        result.append({"metric": _id(item["metric"], "precondition metric"),
                       "operator": op, "value": _number(item["value"], "precondition value")})
    return result


def _condition_outcome(precondition: Mapping[str, object], facts: Mapping[str, Mapping[str, object]]) -> str:
    fact = facts.get(precondition["metric"])
    if fact is None:
        return "UNKNOWN"
    status = fact["status"]
    if status != "OBSERVED":
        return "CONTESTED" if status == "CONTESTED" else "UNKNOWN"
    value = fact["value"]
    if type(value) not in (int, float):
        return "UNKNOWN"
    target = precondition["value"]
    op = precondition["operator"]
    return "MET" if ((op == "EQ" and value == target)
                     or (op == "GE" and value >= target)
                     or (op == "LE" and value <= target)) else "UNMET"


def _parse_alternatives(raw: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence) or len(raw) > _MAX_OPTIONS:
        raise ValueError("alternatives must be bounded sequence")
    seen: set[str] = set()
    parsed = []
    for option in raw:
        if not isinstance(option, Mapping):
            raise ValueError("alternative must be a mapping")
        name = option.get("option_id") or option.get("mission_id") or option.get("capability_id")
        ident = _id(name, "alternative id")
        if ident in seen:
            raise ValueError("duplicate alternative identifier")
        seen.add(ident)
        cost = option.get("cost_units", 0)
        if type(cost) is not int or cost != 0:
            raise ValueError("only zero-cost analytical alternatives are supported")
        assumptions = option.get("assumptions", ())
        if not isinstance(assumptions, (list, tuple)) or len(assumptions) > 16:
            raise ValueError("assumptions must be a bounded list")
        parsed.append({"option_id": ident,
                       "expected_delta": None if option.get("expected_delta") is None else _number(option["expected_delta"], "expected_delta"),
                       "kind": _text(option.get("kind", "ANALYZE"), "alternative kind"),
                       "preconditions": _preconditions(option.get("preconditions", ())),
                       "assumptions": [_text(x, "assumption") for x in assumptions]})
    return parsed


def _parse_hypotheses(raw: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """Normalize rival predictions without converting unknowns to evidence."""
    if not isinstance(raw, (list, tuple)) or len(raw) > 32:
        raise ValueError("hypotheses must be bounded sequence")
    parsed: list[dict[str, object]] = []
    seen: set[str] = set()
    for candidate in raw:
        if not isinstance(candidate, Mapping) or set(candidate) != {"hypothesis_id", "predictions"}:
            raise ValueError("hypothesis fields invalid")
        ident = _id(candidate["hypothesis_id"], "hypothesis_id")
        if ident in seen:
            raise ValueError("duplicate hypothesis_id")
        seen.add(ident)
        predictions = candidate["predictions"]
        if not isinstance(predictions, Mapping) or len(predictions) > 16:
            raise ValueError("predictions must be bounded mapping")
        normalized: dict[str, object] = {}
        for metric, expected in predictions.items():
            key = _id(metric, "predicted metric")
            if type(expected) in (int, float):
                normalized[key] = _number(expected, "predicted value", limit=_MAX_WORLD_ABS)
            elif type(expected) is bool:
                normalized[key] = expected
            elif isinstance(expected, str) and len(expected) <= _MAX_WORLD_TEXT:
                normalized[key] = expected
            else:
                raise ValueError("predicted value must be bounded scalar")
        parsed.append({"hypothesis_id": ident, "predictions": normalized})
    return sorted(parsed, key=lambda x: str(x["hypothesis_id"]))


def _same_prediction(left: object, right: object) -> bool:
    # ``True == 1`` is true in Python, but boolean and numeric predictions
    # represent different evidence types. Numeric int/float are normalized.
    return type(left) is type(right) and left == right


def _same_prediction_profile(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    """Compare complete observable predictions, independent of hypothesis ID."""
    return (set(left) == set(right)
            and all(_same_prediction(left[key], right[key]) for key in left))


class LocalReasoner:
    """Deterministic conditional reasoning, not a language model or authority."""

    def reason(
        self,
        goal: Mapping[str, object],
        world_snapshot: Mapping[str, object],
        alternatives: Sequence[Mapping[str, object]] = (),
    ) -> dict[str, object]:
        """Assess observed target gap, rival analytic options and prerequisites.

        Every projection remains conditional on unverified deltas; no model
        output upgrades a reported event to observed or effect-confirmed.
        """
        parsed_goal = _goal(goal)
        revision, facts = _world(world_snapshot)
        options = _parse_alternatives(alternatives)
        metric = str(parsed_goal["metric"])
        fact = facts.get(metric)
        status = "UNKNOWN" if fact is None else str(fact["status"])
        current = None if fact is None else fact["value"]
        if status == "OBSERVED" and type(current) not in (int, float):
            status, current = "NONNUMERIC_OBSERVATION", None
        target = float(parsed_goal["target_value"])
        direction = str(parsed_goal["direction"])
        gaps = [{"metric": metric, "reason": "CONTESTED_DECLARED_OBSERVATIONS" if status == "CONTESTED" else "NO_ADMISSIBLE_OBSERVED_BASELINE"}] if status != "OBSERVED" else []
        analysis: list[dict[str, object]] = []
        for option in options:
            checks = [{**cond, "result": _condition_outcome(cond, facts)} for cond in option["preconditions"]]
            violated = [c for c in checks if c["result"] == "UNMET"]
            unknown = [c for c in checks if c["result"] in ("UNKNOWN", "CONTESTED")]
            delta = option["expected_delta"]
            projected = None
            progress = None
            if violated:
                assessment = "BLOCKED_BY_PRECONDITION"
            elif unknown:
                assessment = "NEEDS_PRECONDITION_EVIDENCE"
            elif status != "OBSERVED":
                assessment = "NEEDS_BASELINE_EVIDENCE"
            elif delta is None:
                assessment = "EVIDENCE_GATHERING_ONLY"
            else:
                signed = delta if direction == "INCREASE" else -delta
                if signed < 0:
                    assessment = "COUNTERPRODUCTIVE_PROJECTION"
                else:
                    projected = current + delta
                    if not math.isfinite(projected) or abs(projected) > _MAX_WORLD_ABS:
                        assessment = "UNBOUNDED_PROJECTION"
                        projected = None
                    else:
                        remaining = abs(target - current)
                        progress = min(max(0.0, signed), remaining)
                        assessment = "CONDITIONAL_OPTION" if signed else "ANALYSIS_ONLY"
            analysis.append({"option_id": option["option_id"],
                             "assessment": assessment,
                             "conditions": checks,
                             "conditional_projected_value": projected,
                             "conditional_progress": progress,
                             "assumptions": list(option["assumptions"]) +
                                            (["expected_delta is a declared simulation assumption"] if delta is not None else [])})

        if status != "OBSERVED":
            decision = "NEEDS_EVIDENCE"
            recommendation = None
        else:
            reached = (current >= target) if direction == "INCREASE" else (current <= target)
            if reached:
                decision = "OBSERVED_TARGET_REACHED_UNVERIFIED"
                recommendation = None
            else:
                eligible = [x for x in analysis if x["assessment"] == "CONDITIONAL_OPTION"]
                eligible.sort(key=lambda x: (-float(x["conditional_progress"]),
                                             abs(target - float(x["conditional_projected_value"])),
                                             len(x["assumptions"]), str(x["option_id"])))
                recommendation = eligible[0]["option_id"] if eligible else None
                decision = "CONDITIONAL_RECOMMENDATION" if recommendation else "NO_SUPPORTED_OPTION"
        return {"goal_id": parsed_goal["goal_id"], "world_revision": revision,
                "objective": parsed_goal["objective"], "metric": metric, "direction": direction,
                "target_value": target, "observed_value": current,
                "observation_status": status,
                "decision": decision, "recommended_option_id": recommendation,
                "alternatives": analysis, "evidence_gaps": gaps,
                "uncertainty": "DECLARED_OBSERVATIONS_AND_SIMULATED_EFFECTS_UNVERIFIED",
                "effect_status": "NOT_EXECUTED", "authorization": "NOT_GRANTED"}

    def compare_hypotheses(
        self, world_snapshot: Mapping[str, object], hypotheses: Sequence[Mapping[str, object]],
    ) -> list[dict[str, object]]:
        """Compare predictions with *declared* observations; no truth claims."""
        _, facts = _world(world_snapshot)
        result: list[dict[str, object]] = []
        for candidate in _parse_hypotheses(hypotheses):
            ident = str(candidate["hypothesis_id"])
            matched: list[str] = []
            contradicted: list[str] = []
            unknown: list[str] = []
            for key, expected_value in candidate["predictions"].items():
                observed = facts.get(key)
                if observed is None or observed["status"] != "OBSERVED":
                    unknown.append(key)
                elif _same_prediction(observed["value"], expected_value):
                    matched.append(key)
                else:
                    contradicted.append(key)
            classification = ("CONTRADICTED_BY_DECLARED_OBSERVATIONS" if contradicted
                              else "CONSISTENT_WITH_DECLARED_OBSERVATIONS" if matched and not unknown
                              else "UNRESOLVED")
            result.append({"hypothesis_id": ident, "classification": classification,
                           "matched": sorted(matched), "contradicted": sorted(contradicted),
                           "unknown": sorted(unknown), "truth_status": "NOT_ESTABLISHED"})
        result.sort(key=lambda x: (len(x["contradicted"]), -len(x["matched"]), x["hypothesis_id"]))
        return result

    def suggest_discriminating_observations(
        self, world_snapshot: Mapping[str, object],
        hypotheses: Sequence[Mapping[str, object]],
    ) -> dict[str, object]:
        """Suggest *unperformed* observations separating still-viable rivals.

        The score is a count of rival pairs making distinct predictions for
        the same currently unknown/contested metric, not a probability, causal
        forecast, sensor instruction, or authorization to collect information.
        """
        revision, facts = _world(world_snapshot)
        parsed = _parse_hypotheses(hypotheses)
        reports = self.compare_hypotheses(world_snapshot, hypotheses)
        excluded = sorted(str(row["hypothesis_id"]) for row in reports if row["contradicted"])
        viable = [h for h in parsed if h["hypothesis_id"] not in excluded]
        # A profile counts only where *viable rivals actually disagree* on a
        # not-yet-observed metric. An extra prediction for a metric that no
        # other rival predicts cannot discriminate anything. Likewise,
        # already OBSERVED fields cannot be re-probed. Ignoring both classes
        # prevents cloned hypotheses with decorative predictions from
        # multiplying pair counts and changing an otherwise identical rank.
        # Original IDs remain visible, not silently discarded or merged as
        # proof that the underlying causal hypotheses are identical.
        predictions_by_metric: dict[str, list[object]] = {}
        for hypothesis in viable:
            for metric, expected in hypothesis["predictions"].items():
                if facts.get(metric, {}).get("status") != "OBSERVED":
                    predictions_by_metric.setdefault(metric, []).append(expected)
        discriminating_metrics = {
            metric for metric, predicted in predictions_by_metric.items()
            if len(predicted) >= 2 and any(
                not _same_prediction(predicted[0], candidate) for candidate in predicted[1:]
            )
        }
        distinct: list[dict[str, object]] = []
        equivalent_groups: list[list[str]] = []
        effective_profiles: list[dict[str, object]] = []
        for hypothesis in viable:
            effective = {
                metric: expected for metric, expected in hypothesis["predictions"].items()
                if metric in discriminating_metrics
            }
            for index, representative in enumerate(effective_profiles):
                if _same_prediction_profile(effective, representative):
                    equivalent_groups[index].append(str(hypothesis["hypothesis_id"]))
                    break
            else:
                distinct.append(hypothesis)
                effective_profiles.append(effective)
                equivalent_groups.append([str(hypothesis["hypothesis_id"])])
        candidates: dict[str, list[list[str]]] = {}
        if len(distinct) >= 2:
            for index, left in enumerate(distinct):
                for right in distinct[index + 1:]:
                    for metric in sorted(set(left["predictions"]) & set(right["predictions"])):
                        current = facts.get(metric)
                        if current is not None and current["status"] == "OBSERVED":
                            continue
                        if not _same_prediction(left["predictions"][metric],
                                                right["predictions"][metric]):
                            candidates.setdefault(metric, []).append([
                                str(left["hypothesis_id"]), str(right["hypothesis_id"]),
                            ])
        ranked = sorted(candidates.items(), key=lambda item: (-len(item[1]), item[0]))
        observations = [{
            "metric": metric,
            "priority": ("REVERIFY_CONFLICT" if facts.get(metric, {}).get("status") == "CONTESTED"
                         else "MEASURE_UNKNOWN"),
            "distinguished_pair_count": len(pairs),
            "hypothesis_pairs": pairs[:16],
            "pairs_truncated": len(pairs) > 16,
            "evidence_status": "NOT_YET_OBSERVED",
        } for metric, pairs in ranked[:16]]
        status = ("NEEDS_RIVAL_HYPOTHESES" if len(viable) < 2
                  else "PROPOSED_OBSERVATIONS" if observations
                  else "NO_DISCRIMINATING_OBSERVATION")
        return {"world_revision": revision, "status": status,
                "viable_hypothesis_ids": [str(x["hypothesis_id"]) for x in viable],
                "excluded_hypothesis_ids": excluded,
                "observational_equivalence_groups": equivalent_groups,
                "distinct_prediction_profiles": len(distinct),
                "observations": observations,
                "ranking_basis": "COUNT_OF_DISTINCT_UNOBSERVED_PREDICTION_PROFILE_PAIRS_NOT_PROBABILITY",
                "probability_status": "NOT_INFERRED", "authorization": "NOT_GRANTED",
                "effect_status": "NOT_EXECUTED",
                "uncertainty": "HYPOTHESES_AND_OBSERVATIONS_ARE_DECLARED_UNVERIFIED"}

    def respond(self, prompt: str, *, context: Mapping[str, object]) -> dict[str, object]:
        """Produce a multi-turn explanation grounded solely in admitted context.

        Context is the caller's *untrusted* goal/world/history, not a privileged
        command channel. This is a limited natural-language interface to the
        symbolic reasoning above, with transparent non-LLM capabilities.
        """
        question = _text(prompt, "prompt", multiline=True)
        if not isinstance(context, Mapping):
            raise ValueError("context must be a mapping")
        history = context.get("history", [])
        if not isinstance(history, (list, tuple)) or len(history) > _MAX_HISTORY:
            raise ValueError("history must be a bounded list")
        previous_user = ""
        for row in history:
            if not isinstance(row, Mapping) or set(row) != {"role", "content"}:
                raise ValueError("history record must have role and content")
            role = row["role"]
            if role not in ("user", "assistant", "USER", "DIRECTOR"):
                raise ValueError("history role must be user/assistant or USER/DIRECTOR")
            content = _text(row["content"], "history content", multiline=True)
            if role in ("user", "USER"):
                previous_user = content

        goal = context.get("goal")
        world = context.get("world_snapshot")
        if goal is None or world is None:
            return self._answer(
                "I need a declared goal and a world-state snapshot before I can evaluate evidence or alternatives. "
                "I cannot infer those from the conversation alone.",
                "NEEDS_CONTEXT", [], "NO_GROUNDED_CONTEXT")
        alternatives = context.get("alternatives", ())
        outcome = self.reason(goal, world, alternatives)
        metric = outcome["metric"]
        state = outcome["observation_status"]
        current = outcome["observed_value"]
        target = outcome["target_value"]
        direction = outcome["direction"].lower()
        basis = (["world_revision:" + str(outcome["world_revision"])]
                 + ["goal_id:" + str(outcome["goal_id"])])
        lower = question.casefold().strip()
        if lower in ("why?", "why", "explain", "explain that", "how so?", "how so") or lower.startswith("why?\n"):
            lower = (previous_user + " why").casefold() if previous_user else "why is that the status?"
        conditional = re.search(r"\bwhat if\s+(?:the\s+)?(?:[a-zA-Z_][\w]*\s+)?(increase|increases|increased|decrease|decreases|decreased)\s+by\s+(-?\d+(?:\.\d+)?)\b", lower)
        if conditional:
            amount = _number(float(conditional.group(2)), "conditional amount")
            if state != "OBSERVED":
                answer = (f"A change of {_fmt(amount)} to {metric} is only a hypothetical. "
                          f"The current {metric} baseline is {state.lower()}, so I cannot compute a grounded projection.")
                return self._answer(answer, "CONDITIONAL_UNRESOLVED", basis, outcome["uncertainty"])
            delta = amount if conditional.group(1).startswith("increase") else -amount
            estimate = current + delta
            answer = (f"Conditionally, changing {metric} from {_fmt(current)} by {_fmt(delta)} "
                      f"would produce {_fmt(estimate)} toward the declared target {_fmt(target)}. "
                      "This is arithmetic on a hypothetical, not a verified forecast or effect.")
            return self._answer(answer, "CONDITIONAL_SIMULATION", basis, outcome["uncertainty"])

        rivals = context.get("hypotheses", ())
        asks_probe = any(word in lower for word in (
            "distinguish", "discriminat", "test hypoth", "test theor", "which observation",
            "which measurement", "what to measure", "what should we measure",
        ))
        asks_falsifier = "disprov" in lower or "falsif" in lower
        if asks_probe or asks_falsifier:
            if not rivals:
                return self._answer(
                    "No rival hypotheses were supplied. I need declared competing predictions before I can suggest a discriminating observation.",
                    "NEEDS_HYPOTHESES", basis, "HYPOTHESES_MISSING")
            parsed = _parse_hypotheses(rivals)
            if asks_falsifier:
                mentioned = [h for h in parsed if re.search(
                    r"(?<![\w.:-])" + re.escape(str(h["hypothesis_id"]).casefold()) + r"(?![\w.:-])", lower)]
                if len(mentioned) != 1:
                    return self._answer(
                        "Name one declared hypothesis ID to request a conditional falsifier.",
                        "NEEDS_HYPOTHESIS_ID", basis, "HYPOTHESIS_SELECTION_MISSING")
                selected = mentioned[0]
                predictions = sorted(selected["predictions"].items())
                if not predictions:
                    return self._answer(
                        f"Hypothesis {selected['hypothesis_id']} supplies no testable prediction.",
                        "NO_FALSIFIER_SPECIFIED", basis, "PREDICTIONS_MISSING")
                metric_id, predicted = predictions[0]
                rendered = json.dumps(predicted, ensure_ascii=True, allow_nan=False)
                answer = (f"Hypothesis {selected['hypothesis_id']} declares {metric_id}={rendered}. "
                          f"An independently checked observation of {metric_id} with a different value "
                          "would contradict that prediction. That is a conditional falsifier, "
                          "not proof of another hypothesis or permission to collect data.")
                return self._answer(answer, "CONDITIONAL_FALSIFIER", basis,
                                    "PREDICTIONS_AND_EVIDENCE_UNVERIFIED")
            proposed = self.suggest_discriminating_observations(world, rivals)
            suggestions = proposed["observations"]
            if not suggestions:
                return self._answer(
                    "There is no presently supported missing observation that distinguishes the still-viable declared hypotheses. "
                    "More distinct predictions or independently reviewed evidence are needed.",
                    proposed["status"], basis, proposed["uncertainty"])
            top = suggestions[0]
            priority = ("independently rechecking contested" if top["priority"] == "REVERIFY_CONFLICT"
                        else "observing previously unknown")
            answer = (f"The next discriminating test would be {priority} metric {top['metric']}. "
                      f"In the declared rival set it separates {top['distinguished_pair_count']} "
                      "hypothesis pair(s), a comparison count rather than a probability. "
                      "It is not proof of any hypothesis, a measured result, or permission to perform the test.")
            return self._answer(answer, "DISCRIMINATING_OBSERVATION", basis,
                                proposed["uncertainty"])

        if "hypothes" in lower or "theor" in lower:
            h = context.get("hypotheses", ())
            if h:
                compared = self.compare_hypotheses(world, h)
                details = "; ".join(x["hypothesis_id"] + ": " + x["classification"].lower().replace("_", " ") for x in compared)
                return self._answer("Against the declared observations, " + details + ". None is proven true.",
                                    "HYPOTHESIS_COMPARISON", basis, outcome["uncertainty"])
            return self._answer("No rival hypotheses were supplied for a discriminating comparison.",
                                "NEEDS_HYPOTHESES", basis, "HYPOTHESES_MISSING")

        if state == "CONTESTED":
            return self._answer(
                f"The available reports about {metric} conflict. I will not choose a numerical baseline "
                "until that conflict is independently checked; a verification mission is appropriate.",
                "CONTESTED", basis, outcome["uncertainty"])
        if state != "OBSERVED":
            return self._answer(
                f"No declared observation establishes a current value for {metric} "
                f"(status: {state.lower()}). An observation is needed before numerical planning "
                f"toward {_fmt(target)}.",
                "NEEDS_EVIDENCE", basis, outcome["uncertainty"])
        if outcome["decision"] == "OBSERVED_TARGET_REACHED_UNVERIFIED":
            return self._answer(
                f"The declared observation for {metric} is {_fmt(current)}, satisfying the numerical "
                f"target {_fmt(target)}. It still requires independent confirmation before claiming success.",
                "OBSERVED_TARGET_REACHED_UNVERIFIED", basis, outcome["uncertainty"])
        gap = abs(target - current)
        winner = outcome["recommended_option_id"]
        if "alternative" in lower or "choose" in lower or "plan" in lower or "recommend" in lower or "next" in lower or "why" in lower:
            if winner:
                choice = next(x for x in outcome["alternatives"] if x["option_id"] == winner)
                projected = choice["conditional_projected_value"]
                answer = (f"Given declared {metric}={_fmt(current)} and target {_fmt(target)}, "
                          f"the gap is {_fmt(gap)}. Option {winner} is the best admissible "
                          f"conditional alternative in this bounded set: its assumed change projects "
                          f"{_fmt(projected)}. Preconditions and actual effects remain unverified.")
                return self._answer(answer, "CONDITIONAL_RECOMMENDATION", basis, outcome["uncertainty"])
            return self._answer(
                f"Given declared {metric}={_fmt(current)} toward {_fmt(target)}, the gap is "
                f"{_fmt(gap)}. No supplied alternative currently satisfies the known planning "
                "conditions; I would request evidence or another admissible analytical option.",
                "NO_SUPPORTED_OPTION", basis, outcome["uncertainty"])
        return self._answer(
            f"The declared {metric} value is {_fmt(current)}; the objective is to {direction} "
            f"it toward {_fmt(target)}, leaving a gap of {_fmt(gap)}. "
            "These are caller-declared observations, not independently authenticated measurements.",
            "GROUNDED_STATUS", basis, outcome["uncertainty"])

    @staticmethod
    def _answer(answer: str, status: str, basis: list[str], uncertainty: str) -> dict[str, object]:
        return {"answer": answer, "status": status, "basis": basis,
                "uncertainty": uncertainty, "model_kind": "LOCAL_SYMBOLIC",
                "effect_status": "NOT_EXECUTED", "source_authenticated": False}


__all__ = ["LocalReasoner"]
