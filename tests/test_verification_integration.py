"""Confirm real old-code failures across three independent PostgreSQL matrices."""

import os

import pytest

from skewguard.config import RunConfig
from skewguard.exec.db import managed_postgres
from skewguard.exec.envs import PythonEnvironment
from skewguard.exec.types import Cell, VerificationResult
from skewguard.exec.verify import verify_matrix

pytestmark = pytest.mark.skipif(
    os.environ.get("SKEWGUARD_TEST_POSTGRES") != "1",
    reason="set SKEWGUARD_TEST_POSTGRES=1 to run tests requiring Docker",
)


def test_rename_failures_reproduce_twice_on_fresh_database_state(
    shop_releases: tuple[RunConfig, dict[str, PythonEnvironment]],
) -> None:
    config, releases = shop_releases
    with managed_postgres(config) as server:
        result = verify_matrix(
            config,
            releases["base"],
            releases["rename"],
            server,
            settings_module="shop.settings",
            seed_fixture="products",
        )

    assert len(result.decisions) == 4
    assert all(item.status == "confirmed" for item in result.decisions)
    assert [attempt.number for attempt in result.attempts] == [1, 2]
    assert result.initial_comparison.issues == []
    matrices = [result.initial_cells]
    for attempt in result.attempts:
        assert attempt.error is None
        assert attempt.comparison is not None and attempt.comparison.issues == []
        assert attempt.cells is not None
        matrices.append(attempt.cells)
    for cells in matrices:
        assert [cell.cell for cell in cells] == list(Cell)
        reads = [
            outcome
            for outcome in cells[0].outcomes
            if outcome.scenario_id == "orm:catalog.Product:read"
        ]
        assert len(reads) == 1
        assert isinstance(reads[0].body, list) and len(reads[0].body) == 2
    assert VerificationResult.model_validate_json(result.model_dump_json()) == result
