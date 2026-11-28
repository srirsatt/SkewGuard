"""Validate the shape of model explanations for confirmed divergences."""

from collections.abc import Callable

import pytest
from pydantic import ValidationError

from skewguard.agents.triage import TriageExplanation
from skewguard.exec.types import DivergenceCategory
from skewguard.report.json_report import RunReport


def explanation(**changes: object) -> TriageExplanation:
    return TriageExplanation.model_validate({
        "cause_operation": "RenameField",
        "migration_file": "catalog/migrations/0002_change.py",
        "code_location": {"path": "catalog/views.py", "line": 18},
        "category": "crash",
        "explanation": "The old view still selects the renamed price column.",
        "confidence": 0.9,
        **changes,
    })


def test_citation_and_category_survive_json_round_trip() -> None:
    result = explanation()
    assert result.category is DivergenceCategory.CRASH
    assert result.code_location and result.code_location.line == 18
    assert TriageExplanation.model_validate_json(result.model_dump_json()) == result


def test_missing_evidence_can_be_expressed_without_a_citation() -> None:
    result = explanation(
        cause_operation=None, migration_file=None, code_location=None,
        explanation="PostgreSQL reported a missing column. Source was unavailable.",
    )
    assert result.code_location is None


@pytest.mark.parametrize("changes", [
    {"code_location": {"path": "catalog/views.py"}},
    {"code_location": {"path": "catalog/views.py", "line": 0}},
    {"code_location": {"path": " ", "line": 18}},
    {"category": "looks unsafe"},
    {"confidence": 1.1},
    {"explanation": " "},
    {"verdict": "break_detected"},
])
def test_malformed_explanations_are_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        explanation(**changes)


def test_model_cannot_reclassify_a_confirmed_crash(
    report_factory: Callable[[str], RunReport],
) -> None:
    finding = report_factory("breaking").verification.decisions[0]
    assert TriageExplanation.from_finding(explanation().model_dump_json(), finding)
    reply = explanation(category="body_change", confidence=0.95).model_dump_json()
    with pytest.raises(ValueError, match="execution recorded crash"):
        TriageExplanation.from_finding(reply, finding)


def test_flaky_failure_cannot_be_triaged(
    report_factory: Callable[[str], RunReport],
) -> None:
    finding = report_factory("flaky").verification.decisions[0]
    with pytest.raises(ValueError, match="requires a confirmed failure"):
        TriageExplanation.from_finding(explanation().model_dump_json(), finding)
