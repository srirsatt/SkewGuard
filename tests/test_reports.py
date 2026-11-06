"""Keep reports faithful to execution evidence, uncertainty, and replay errors."""

from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from skewguard.report import json_report
from skewguard.report.json_report import RunReport, read_json_report, write_json_report
from skewguard.report.markdown import render_markdown


@pytest.mark.parametrize(
    ("mode", "status", "confirmed", "flaky", "inconclusive"),
    [
        ("safe", "no_break_detected", 0, 0, 0),
        ("breaking", "break_detected", 2, 0, 0),
        ("flaky", "inconclusive", 0, 2, 0),
        ("inconclusive", "inconclusive", 0, 0, 2),
        ("coverage", "inconclusive", 0, 0, 0),
        ("head_failure", "inconclusive", 0, 0, 0),
    ],
)
def test_summary_distinguishes_reproduced_breaks_from_missing_evidence(
    report_factory: Callable[[str], RunReport],
    mode: str,
    status: str,
    confirmed: int,
    flaky: int,
    inconclusive: int,
) -> None:
    report = report_factory(mode)

    assert report.summary.status == status
    assert report.summary.confirmed == confirmed
    assert report.summary.flaky == flaky
    assert report.summary.inconclusive == inconclusive
    assert report.summary.deploy_window == status
    assert report.summary.rollback == status


def test_replay_only_failure_is_unverified_and_blocks_a_clear_rollback_result(
    report_factory: Callable[[str], RunReport],
) -> None:
    report = report_factory("replay_only")

    assert report.summary.status == "break_detected"
    assert report.summary.deploy_window == "break_detected"
    assert report.summary.rollback == "inconclusive"
    assert report.summary.confirmed == 1
    assert report.summary.unverified == 1
    assert "First observed during replay" in render_markdown(report)


def test_body_policy_is_preserved_and_rollback_is_summarized_independently(
    report_factory: Callable[[str], RunReport],
) -> None:
    report = report_factory("body_change")

    assert report.summary.deploy_window == "break_detected"
    assert report.summary.rollback == "no_break_detected"
    assert report.summary.confirmed == 1
    policy = report.context.comparison_policies["orm:catalog.Product:read"]
    assert policy.compare_body
    assert policy.volatile_fields == frozenset({"id"})
    assert RunReport.model_validate_json(report.model_dump_json()) == report


def test_json_roundtrip_preserves_full_pinned_evidence_and_creates_output_directory(
    report_factory: Callable[[str], RunReport],
    tmp_path: Path,
) -> None:
    report = report_factory("breaking")
    path = tmp_path / "reports/out.json"

    assert write_json_report(report, path) == path
    assert read_json_report(path) == report
    assert read_json_report(path).context.base_sha == "a" * 40
    assert len(read_json_report(path).verification.attempts) == 2
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_saved_report_does_not_require_its_original_repository(
    report_factory: Callable[[str], RunReport],
    tmp_path: Path,
) -> None:
    report = report_factory("safe")
    report = report.model_copy(
        update={
            "context": report.context.model_copy(
                update={"repo_path": tmp_path / "deleted-repository"}
            )
        }
    )
    path = write_json_report(report, tmp_path / "portable.json")

    assert not report.context.repo_path.exists()
    assert read_json_report(path) == report


@pytest.mark.parametrize("part", ["summary", "decision", "initial", "replay"])
def test_loading_rejects_labels_or_comparisons_that_conflict_with_raw_evidence(
    report_factory: Callable[[str], RunReport],
    part: str,
) -> None:
    data = report_factory("flaky").model_dump(mode="json")
    if part == "summary":
        data["summary"]["status"] = "no_break_detected"
    elif part == "decision":
        data["verification"]["decisions"][0]["status"] = "confirmed"
    elif part == "initial":
        data["verification"]["initial_comparison"]["candidates"][0]["category"] = (
            "body_change"
        )
    else:
        data["verification"]["attempts"][1]["comparison"]["candidates"][0][
            "category"
        ] = "body_change"

    with pytest.raises(ValidationError):
        RunReport.model_validate(data)


def test_unknown_report_schema_is_rejected(
    report_factory: Callable[[str], RunReport],
) -> None:
    data = report_factory("safe").model_dump(mode="json")
    data["schema_version"] = "2"

    with pytest.raises(ValidationError, match="schema_version"):
        RunReport.model_validate(data)


def test_failed_atomic_write_preserves_existing_output_and_removes_temporary_file(
    report_factory: Callable[[str], RunReport],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "out.json"
    path.write_text("previous output", encoding="utf-8")
    monkeypatch.setattr(
        json_report.os, "replace", Mock(side_effect=OSError("disk failure"))
    )

    with pytest.raises(OSError, match="disk failure"):
        write_json_report(report_factory("breaking"), path)

    assert path.read_text(encoding="utf-8") == "previous output"
    assert list(tmp_path.glob(".skewguard-*")) == []


def test_markdown_shows_confirmed_evidence_without_claiming_universal_safety(
    report_factory: Callable[[str], RunReport],
) -> None:
    markdown = render_markdown(report_factory("breaking"))

    assert "Reproducible break detected" in markdown
    assert "A0D1w" in markdown
    assert "ProgrammingError" in markdown
    assert 'column "price" does not exist' in markdown
    assert "both fresh replays" in markdown
    assert "do not prove migration safety" in markdown
    assert "a" * 40 in markdown and "b" * 40 in markdown


def test_markdown_retains_flaky_failures_replay_errors_and_health_issues(
    report_factory: Callable[[str], RunReport],
) -> None:
    assert "| flaky |" in render_markdown(report_factory("flaky"))
    incomplete = render_markdown(report_factory("inconclusive"))
    assert "Replay execution errors" in incomplete
    assert "database unavailable" in incomplete
    head_failure = render_markdown(report_factory("head_failure"))
    assert "Coverage and health issues" in head_failure
    assert "A1D1" in head_failure and "ProgrammingError" in head_failure
    assert "No break detected in executed checks" in render_markdown(
        report_factory("safe")
    )


def test_markdown_escapes_html_links_and_multiline_table_evidence(
    report_factory: Callable[[str], RunReport],
) -> None:
    markdown = render_markdown(report_factory("hostile"))

    assert "<script>" not in markdown
    assert "&lt;script&gt;" in markdown
    assert "[link](https://example.invalid)" not in markdown
    assert r"\[link\]" in markdown
    assert r"\|" in markdown
    assert "<br>next row" in markdown
