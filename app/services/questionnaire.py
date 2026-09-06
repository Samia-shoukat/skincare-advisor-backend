"""
Skin type scoring. FR-ONB-006.

This module is an interpreter, not a decision-maker. Every weight, threshold,
and tie-break lives in `app/clinical/questionnaire/skin_type.v0.json`. If you
find yourself adding `if question_id == "Q3"` here, the logic belongs in the
config instead -- CON-003 keeps clinical judgement out of application code, and
the whole point of the split is that a reviewer who does not read Python can
still check the instrument.

FR-ONB-006 requires determinism: the same answers always produce the same type.
Nothing here reads a clock, a random source, or the database. The only input is
the answer map and the loaded config.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.enums import SkinType

logger = logging.getLogger(__name__)


class QuestionnaireInvalid(ValueError):
    """The config file is malformed. Raised at load, not at first user."""


class IncompleteAnswers(ValueError):
    """A question was unanswered or an option id was not recognised."""


@dataclass(frozen=True)
class Option:
    option_id: str
    label: str
    scores: dict[str, int]


@dataclass(frozen=True)
class Question:
    question_id: str
    prompt: str
    options: tuple[Option, ...]

    def option(self, option_id: str) -> Option | None:
        return next((o for o in self.options if o.option_id == option_id), None)


@dataclass(frozen=True)
class ScoringRule:
    rule_id: str
    result: SkinType
    conditions: dict[str, int]

    def matches(self, totals: dict[str, int]) -> bool:
        """
        Every condition is a minimum on some derived quantity. A rule with no
        conditions matches everything, which is how the final fallback works.
        """
        for name, floor in self.conditions.items():
            if _derive(name, totals) < floor:
                return False
        return True


@dataclass(frozen=True)
class Questionnaire:
    version: str
    questions: tuple[Question, ...]
    rules: tuple[ScoringRule, ...]

    def question(self, question_id: str) -> Question | None:
        return next((q for q in self.questions if q.question_id == question_id), None)

    def as_payload(self) -> dict[str, Any]:
        """
        What GET /v1/content/questionnaire returns.

        Scores are stripped. A client that can see the weights can work
        backwards to a chosen answer, which would let a user pick their skin
        type rather than answer honestly -- and the routine is built on it.
        """
        return {
            "version": self.version,
            "questions": [
                {
                    "id": q.question_id,
                    "prompt": q.prompt,
                    "options": [{"id": o.option_id, "label": o.label} for o in q.options],
                }
                for q in self.questions
            ],
        }


def _derive(name: str, totals: dict[str, int]) -> int:
    """
    Resolve a condition name to a number.

    Two of these are differences rather than raw totals. Comparing a margin
    instead of an absolute score is what stops a near-tie being reported as a
    definite classification.
    """
    if name == "oil_minus_dry_min":
        return totals.get("oil", 0) - totals.get("dry", 0)
    if name == "dry_minus_oil_min":
        return totals.get("dry", 0) - totals.get("oil", 0)
    if name == "tzone_min":
        return totals.get("tzone", 0)
    if name == "cheek_dry_min":
        return totals.get("cheek_dry", 0)
    if name == "oil_min":
        return totals.get("oil", 0)
    if name == "dry_min":
        return totals.get("dry", 0)
    raise QuestionnaireInvalid(f"unknown scoring condition {name!r}")


def load_questionnaire(path: Path) -> Questionnaire:
    payload = json.loads(path.read_text(encoding="utf-8"))

    version = payload.get("version")
    if not version:
        raise QuestionnaireInvalid("questionnaire has no version")

    questions: list[Question] = []
    for raw_q in payload.get("questions", []):
        options = tuple(
            Option(
                option_id=raw_o["id"],
                label=raw_o["label"],
                scores={k: int(v) for k, v in (raw_o.get("scores") or {}).items()},
            )
            for raw_o in raw_q.get("options", [])
        )
        if len(options) < 2:
            raise QuestionnaireInvalid(f"question {raw_q.get('id')!r} has fewer than two options")
        questions.append(
            Question(question_id=raw_q["id"], prompt=raw_q["prompt"], options=options)
        )

    if not questions:
        raise QuestionnaireInvalid("questionnaire has no questions")

    rules: list[ScoringRule] = []
    for raw_r in payload.get("scoring", {}).get("rules", []):
        try:
            result = SkinType(raw_r["result"])
        except ValueError as exc:
            raise QuestionnaireInvalid(
                f"rule {raw_r.get('ruleId')!r} names a result outside the SkinType enumeration"
            ) from exc
        rules.append(
            ScoringRule(
                rule_id=raw_r["ruleId"],
                result=result,
                conditions={k: int(v) for k, v in (raw_r.get("conditions") or {}).items()},
            )
        )

    if not rules:
        raise QuestionnaireInvalid("questionnaire has no scoring rules")

    # A total function is a hard requirement: FR-ONB-006 says the questionnaire
    # always yields one of the four types, so some rule must match every
    # possible answer set. An unconditional final rule is the only way to
    # guarantee that, and checking it here is cheaper than discovering the gap
    # when a real user falls through.
    if rules[-1].conditions:
        raise QuestionnaireInvalid(
            "the last scoring rule must be unconditional so that every answer set resolves"
        )

    return Questionnaire(version=version, questions=tuple(questions), rules=tuple(rules))


@lru_cache
def get_questionnaire(path_str: str) -> Questionnaire:
    """Cached per path. The file is read once per process."""
    return load_questionnaire(Path(path_str))


def score(questionnaire: Questionnaire, answers: dict[str, str]) -> tuple[SkinType, dict[str, int]]:
    """
    Turn answers into a skin type.

    Returns the type and the score totals. The totals are returned for logging
    and for the FYP evidence trail -- they are not sent to the client, for the
    same reason the weights are not.

    Raises `IncompleteAnswers` if any question is missing or any option id is
    unrecognised. Partial scoring is not offered: a type derived from four of
    six answers looks identical to one derived from all six, and there would be
    no way afterwards to tell them apart.
    """
    totals: dict[str, int] = {}

    for question in questionnaire.questions:
        chosen_id = answers.get(question.question_id)
        if chosen_id is None:
            raise IncompleteAnswers(f"no answer for {question.question_id}")

        option = question.option(chosen_id)
        if option is None:
            raise IncompleteAnswers(
                f"{chosen_id!r} is not an option for {question.question_id}"
            )

        for axis, points in option.scores.items():
            totals[axis] = totals.get(axis, 0) + points

    unexpected = set(answers) - {q.question_id for q in questionnaire.questions}
    if unexpected:
        # Usually a stale client running against a newer questionnaire version.
        raise IncompleteAnswers(f"unrecognised question ids: {sorted(unexpected)}")

    for rule in questionnaire.rules:
        if rule.matches(totals):
            logger.info("skin type resolved by rule %s", rule.rule_id)
            return rule.result, totals

    # Unreachable: load_questionnaire refuses a config whose last rule is
    # conditional. Kept so that a future change to that check fails loudly here
    # rather than returning None to a caller expecting a SkinType.
    raise QuestionnaireInvalid("no scoring rule matched; the fallback rule is missing")