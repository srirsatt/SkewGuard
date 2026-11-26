"""Verify post-write rollback comparisons, setup failures, and qualification gaps."""

import os
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from scripts.make_demo_repo import git, make_demo_repo
from skewguard.config import RunConfig
from skewguard.exec.diff import compare_matrix
from skewguard.exec.scenarios import Scenario
from skewguard.exec.types import Cell, CellResult, ComparisonIssueKind, Outcome
from skewguard.graph.session import run_session

PLAN = TypeAdapter(tuple[Scenario, ...]).validate_json(
    (
        Path(__file__).resolve().parents[1] / "examples/shop_http_scenarios.json"
    ).read_bytes()
)
READ, WRITE = PLAN


def test_duplicate_http_ids_fail_at_input(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="IDs must be unique"):
        RunConfig(
            repo_path=tmp_path, base_ref="base", head_ref="head", scenarios=(READ, READ)
        )


def test_failed_head_write_is_rollback_setup_evidence() -> None:
    read = Outcome(
        scenario_id=READ.scenario_id,
        status="http_status",
        http_status=200,
        body={"products": []},
    )
    write = Outcome(
        scenario_id=WRITE.scenario_id, status="http_status", http_status=201
    )
    bad_write = write.model_copy(update={"http_status": 400})
    commit = Outcome(scenario_id="orm:catalog.Product:commit", status="ok")
    cells = [
        CellResult(
            cell=Cell.A0D0,
            outcomes=[read, write],
            qualified_scenarios=PLAN,
            rollback_controls=(read,),
            rollback_writes=(write,),
        ),
        CellResult(cell=Cell.A0D1, outcomes=[read, write]),
        CellResult(cell=Cell.A1D1, outcomes=[read, bad_write]),
        CellResult(
            cell=Cell.A0D1W, outcomes=[commit, read], rollback_writes=(bad_write,)
        ),
    ]
    comparison = compare_matrix(cells)
    assert comparison.candidates == []
    assert {(issue.cell, issue.kind) for issue in comparison.issues} == {
        (Cell.A0D1W, ComparisonIssueKind.ROLLBACK_SETUP_FAILED),
        (Cell.A1D1, ComparisonIssueKind.NEW_CODE_FAILURE),
    }
    assert all(issue.outcome == bad_write for issue in comparison.issues)


@pytest.mark.skipif(
    os.environ.get("SKEWGUARD_TEST_POSTGRES") != "1", reason="requires Docker"
)
def test_only_old_reader_after_new_writer_breaks(tmp_path: Path) -> None:
    repo = make_demo_repo(tmp_path / "shop")
    git(repo, "checkout", "-b", "legacy", "base")
    view = repo / "catalog/views.py"
    original = view.read_text(encoding="utf-8")
    view.write_text(
        original.replace(
            "    return JsonResponse(\n",
            '    if Product.objects.filter(sku="MUG", stock=0).exists():\n'
            '        raise ValueError("legacy reader cannot handle backorders")\n'
            "    return JsonResponse(\n",
            1,
        ),
        encoding="utf-8",
    )
    git(repo, "commit", "-am", "Read only in-stock mugs on the legacy release")
    git(repo, "checkout", "-b", "new-writer", "legacy")
    git(repo, "checkout", "safe", "--", "catalog/migrations/0002_change.py")
    view.write_text(
        original.replace(
            "    product = Product(**data)",
            '    data["stock"] = 0\n    product = Product(**data)',
        ),
        encoding="utf-8",
    )
    git(repo, "commit", "-am", "Allow newly created products to be backordered")
    rejected = READ.model_copy(
        update={"scenario_id": "http:missing", "path": "/absent/"}
    )
    report = run_session(
        RunConfig(
            repo_path=repo,
            base_ref="legacy",
            head_ref="new-writer",
            scenarios=(*PLAN, rejected),
        ),
        settings_module="shop.settings",
        seed_fixture="products",
    )
    verification = report.verification
    assert [
        (d.candidate.cell, d.candidate.scenario_id, d.status)
        for d in verification.decisions
    ] == [(Cell.A0D1W, READ.scenario_id, "confirmed")], verification.initial_comparison
    assert [
        (issue.cell, issue.scenario_id)
        for issue in verification.initial_comparison.issues
    ] == [(Cell.A0D0, rejected.scenario_id)]
    assert all(
        rejected.scenario_id not in {o.scenario_id for o in cell.outcomes}
        for cell in verification.initial_cells
    )
    for attempt in verification.attempts:
        assert attempt.comparison and attempt.comparison.issues == []
        assert (
            attempt.comparison.candidates == verification.initial_comparison.candidates
        )
