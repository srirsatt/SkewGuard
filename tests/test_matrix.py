"""Verify matrix cell isolation, code selection, and persisted rollback writes."""

import os

import pytest

from skewguard.config import RunConfig
from skewguard.exec.db import managed_postgres
from skewguard.exec.diff import compare_matrix
from skewguard.exec.envs import PythonEnvironment
from skewguard.exec.matrix import run_matrix
from skewguard.exec.types import Cell, DivergenceCategory

pytestmark = pytest.mark.skipif(
    os.environ.get("SKEWGUARD_TEST_POSTGRES") != "1",
    reason="set SKEWGUARD_TEST_POSTGRES=1 to run tests requiring Docker",
)


def test_matrix_uses_four_independent_cells_and_keeps_new_writes(
    shop_releases: tuple[RunConfig, dict[str, PythonEnvironment]],
) -> None:
    config, releases = shop_releases
    with managed_postgres(config) as server:
        for release in ("nullable", "rename", "required"):
            results = run_matrix(
                config,
                releases["base"],
                releases[release],
                server,
                settings_module="shop.settings",
                seed_fixture="products",
            )
            assert [result.cell for result in results] == list(Cell)
            by_cell = {
                result.cell: {
                    outcome.scenario_id: outcome for outcome in result.outcomes
                }
                for result in results
            }
            read = "orm:catalog.Product:read"
            write = "orm:catalog.Product:write"
            committed = "orm:catalog.Product:commit"
            assert all(
                outcome.status == "ok" for outcome in by_cell[Cell.A0D0].values()
            )
            assert all(
                outcome.status == "ok" for outcome in by_cell[Cell.A1D1].values()
            )
            assert by_cell[Cell.A0D1W][committed].status == "ok"
            assert by_cell[Cell.A0D1W][committed].body == {
                "created": True,
                "rolled_back": False,
            }
            assert committed not in by_cell[Cell.A0D0]
            assert committed not in by_cell[Cell.A0D1]
            assert committed not in by_cell[Cell.A1D1]

            comparison = compare_matrix(results)
            assert comparison.issues == []
            expected_count = {"nullable": 0, "rename": 4, "required": 2}[release]
            assert len(comparison.candidates) == expected_count
            assert {item.cell for item in comparison.candidates} == (
                set() if release == "nullable" else {Cell.A0D1, Cell.A0D1W}
            )
            if release == "required":
                assert all(
                    item.scenario_id == write
                    and item.category == DivergenceCategory.INTEGRITY_ERROR
                    for item in comparison.candidates
                )
            elif release == "rename":
                assert all(
                    item.category == DivergenceCategory.CRASH
                    for item in comparison.candidates
                )

            if release == "nullable":
                assert all(
                    outcome.status == "ok"
                    for result in results
                    for outcome in result.outcomes
                )
                baseline_rows = by_cell[Cell.A0D0][read].body
                rollback_rows = by_cell[Cell.A0D1W][read].body
                assert isinstance(baseline_rows, list) and len(baseline_rows) == 2
                assert isinstance(rollback_rows, list) and len(rollback_rows) == 3
                # Probe writes in other cells must not advance snapshot sequences.
                identifiers: set[int] = set()
                for row in rollback_rows:
                    assert isinstance(row, dict)
                    identifier = row["id"]
                    assert type(identifier) is int
                    identifiers.add(identifier)
                assert identifiers == {1, 2, 3}
            elif release == "rename":
                for cell in (Cell.A0D1, Cell.A0D1W):
                    assert by_cell[cell][read].status == "exception"
                    assert by_cell[cell][write].status == "exception"
            else:
                for cell in (Cell.A0D1, Cell.A0D1W):
                    assert by_cell[cell][read].status == "ok"
                    assert by_cell[cell][write].integrity_error
