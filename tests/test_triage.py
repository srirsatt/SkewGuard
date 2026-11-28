"""Validate the shape of model explanations for confirmed divergences."""

from collections.abc import Callable

import pytest
from pydantic import ValidationError

from skewguard.agents.triage import TriageExplanation
from skewguard.exec.types import DivergenceCategory
from skewguard.report.json_report import RunReport

OLD_CODE = {"catalog/views.py": {"18": 'queryset.values("price")'}}


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
    assert TriageExplanation.from_finding(
        explanation().model_dump_json(), finding, old_code=OLD_CODE
    )
    reply = explanation(category="body_change", confidence=0.95).model_dump_json()
    with pytest.raises(ValueError, match="execution recorded crash"):
        TriageExplanation.from_finding(reply, finding, old_code=OLD_CODE)


@pytest.mark.parametrize("old_code", [
    {},
    {"catalog/views.py": {"10": "return response"}},
    {
        "catalog/models.py": {"11": "class Product:"},
        "catalog/views.py": {"10": "return response"},
    },
])
def test_correct_category_cannot_rescue_an_unsupplied_citation(
    report_factory: Callable[[str], RunReport], old_code: dict[str, dict[str, str]],
) -> None:
    finding = report_factory("breaking").verification.decisions[0]
    reply = explanation(code_location={"path": "catalog/models.py", "line": 10})
    with pytest.raises(ValueError, match=r"catalog/models\.py:10"):
        TriageExplanation.from_finding(
            reply.model_dump_json(), finding, old_code=old_code
        )


def test_empty_excerpt_accepts_a_null_citation(
    report_factory: Callable[[str], RunReport],
) -> None:
    finding = report_factory("breaking").verification.decisions[0]
    reply = explanation(code_location=None).model_dump_json()
    parsed = TriageExplanation.from_finding(reply, finding, old_code={})
    assert parsed.code_location is None


def test_flaky_failure_cannot_be_triaged(
    report_factory: Callable[[str], RunReport],
) -> None:
    finding = report_factory("flaky").verification.decisions[0]
    with pytest.raises(ValueError, match="requires a confirmed failure"):
        TriageExplanation.from_finding(
            explanation().model_dump_json(), finding, old_code={}
        )
