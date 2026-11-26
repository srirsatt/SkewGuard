"""Exercise seed routing, shared budgets, real inserts and checkpoint recovery."""

import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langgraph.runtime import Runtime
from typer.testing import CliRunner

from scripts.make_demo_repo import make_demo_repo
from skewguard.agents import generation
from skewguard.agents.scenario import ScenarioProposal
from skewguard.agents.seed import (
    SEED_SYSTEM_PROMPT,
    SeedAttempt,
    SeedDraft,
    SeedProposal,
)
from skewguard.cli import app
from skewguard.config import RunConfig
from skewguard.exec.db import DatabaseConnection
from skewguard.exec.envs import PythonEnvironment
from skewguard.exec.probes import ProbeExecutionError
from skewguard.exec.scenarios import Scenario
from skewguard.exec.types import Cell, Outcome
from skewguard.graph.nodes import scenario as scenario_node
from skewguard.graph.nodes import seed as seed_node
from skewguard.graph.runtime import RunResources
from skewguard.graph.state import RunState
from skewguard.report.json_report import read_json_report

GET = Scenario(
    scenario_id="http:out-of-stock",
    method="GET",
    kind="read",
    path="/products/?out_of_stock=1",
    compare_fields=["products"],
)
MUG = SeedProposal(
    model="catalog.Product",
    fields={
        "sku": "SEED-MUG",
        "name": "Mug",
        "price": 5,
        "stock": 0,
    },
)


@pytest.mark.parametrize(
    "reply",
    [
        '{"model":"Product"}',
        '{"model":"catalog.Product","fields":{"nested":[NaN]}}',
    ],
)
def test_malformed_seed_uses_shared_budget_and_returns_to_pending_get(
    graph_inputs: tuple[RunState, RunResources],
    reply: str,
) -> None:
    state, resources = graph_inputs
    config = state.config.model_copy(update={"seed_model": "local", "max_llm_calls": 2})
    state = state.model_copy(
        update={
            "config": config,
            "scenario_pending": True,
            "proposal_reply": ScenarioProposal(
                scenario=GET, reason="read"
            ).model_dump_json(),
            "seed_draft": SeedDraft.parse_reply(GET.scenario_id, reply),
        }
    )
    assert scenario_node.after_proposal(state) == "propose_seed"
    update = seed_node.validate_seed(state, Runtime(context=resources))
    updated = state.model_copy(update=dict(update))
    assert updated.config.generated_seed is None
    assert "Invalid seed proposal" in updated.seed_attempts[0].detail
    assert updated.remaining_model_calls == 0
    assert seed_node.after_seed(updated) == "qualify_proposal"


def test_seed_requires_a_get_and_stops_after_three_retries(
    graph_inputs: tuple[RunState, RunResources],
) -> None:
    state, _ = graph_inputs
    config = state.config.model_copy(
        update={"seed_model": "local", "max_llm_calls": 20}
    )
    state = state.model_copy(update={"config": config})
    assert scenario_node.after_analysis(state) == "execute_matrix"
    config = config.model_copy(update={"scenarios": (GET,)})
    state = state.model_copy(update={"config": config})
    assert scenario_node.after_analysis(state) == "propose_seed"
    failed = SeedAttempt(
        status="rejected", scenario_id=GET.scenario_id, detail="bad row"
    )
    for count in (1, 2, 3, 4):
        tried = state.model_copy(update={"seed_attempts": (failed,) * count})
        assert seed_node.after_seed(tried) == (
            "propose_seed" if count < 4 else "execute_matrix"
        )


def test_no_call_left_after_scenario_proposal_skips_seed_generation(
    graph_inputs: tuple[RunState, RunResources],
) -> None:
    state, _ = graph_inputs
    config = state.config.model_copy(update={"seed_model": "local", "max_llm_calls": 1})
    state = state.model_copy(
        update={
            "config": config,
            "scenario_pending": True,
            "proposal_reply": ScenarioProposal(
                scenario=GET, reason="read"
            ).model_dump_json(),
        }
    )
    assert scenario_node.after_proposal(state) == "qualify_proposal"


@pytest.mark.skipif(
    os.environ.get("SKEWGUARD_TEST_POSTGRES") != "1", reason="requires Docker"
)
def test_seed_repair_feeds_the_generated_get_and_each_fresh_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_demo_repo(tmp_path / "shop")
    output = tmp_path / "report.json"
    files = ["--checkpoint", str(tmp_path / "runs.sqlite"), "--json", str(output)]
    duplicate = SeedProposal(model=MUG.model, fields={**MUG.fields, "sku": "PEN"})
    scenario = ScenarioProposal(scenario=GET, reason="Read out-of-stock products")
    fake = FakeListChatModel(
        responses=[
            scenario.model_dump_json(),
            duplicate.model_dump_json(),
            MUG.model_dump_json(),
        ]
    )
    initialize = Mock(return_value=fake)
    monkeypatch.setattr(generation, "init_chat_model", initialize)
    # Stop after the first seed draft has been checkpointed.
    insert = seed_node.insert_seed_proposal
    monkeypatch.setattr(
        seed_node,
        "insert_seed_proposal",
        Mock(side_effect=ProbeExecutionError("interrupted")),
    )
    runner = CliRunner()
    first = runner.invoke(
        app,
        [
            "run",
            "--repo",
            str(repo),
            "--base",
            "base",
            "--head",
            "breaking",
            "--settings",
            "shop.settings",
            "--seed",
            "products",
            "--seed-model",
            "local",
            "--scenario-model",
            "local",
            "--max-llm-calls",
            "3",
            *files,
        ],
    )
    assert first.exit_code == 2 and "interrupted" in first.output, (
        first.output,
        first.exception,
    )
    assert initialize.call_count == 2
    run_id = next(
        line.split()[-1]
        for line in first.output.splitlines()
        if line.startswith("Run ID:")
    )
    monkeypatch.setattr(seed_node, "insert_seed_proposal", insert)
    repair = Mock(wraps=seed_node.generate_reply)
    monkeypatch.setattr(seed_node, "generate_reply", repair)
    qualify = scenario_node.qualify_scenario

    def check_seeded_get(
        config: RunConfig,
        base: PythonEnvironment,
        old_snapshot: DatabaseConnection,
        *,
        settings_module: str,
        scenario: Scenario,
    ) -> Outcome:
        result = qualify(
            config,
            base,
            old_snapshot,
            settings_module=settings_module,
            scenario=scenario,
        )
        assert isinstance(result, Outcome) and result.body == {"products": [MUG.fields]}
        return result

    check = Mock(side_effect=check_seeded_get)
    monkeypatch.setattr(scenario_node, "qualify_scenario", check)
    resumed = runner.invoke(app, ["resume", run_id, *files])
    assert resumed.exit_code == 1 and output.exists(), (
        resumed.output,
        resumed.exception,
    )
    repair.assert_called_once()
    turn = repair.call_args
    assert turn.kwargs["system_prompt"] == SEED_SYSTEM_PROMPT
    assert turn.kwargs["previous"] == duplicate.model_dump_json()
    assert "sku" in turn.kwargs["feedback"].lower()
    evidence = json.loads(turn.args[2])
    assert evidence["proposed_request"]["path"] == GET.path
    assert evidence["response_json_schema"] == SeedProposal.model_json_schema()
    assert "catalog/models.py" in evidence["old_release_source"]
    check.assert_called_once()
    assert check.call_args.args[0].generated_seed == MUG
    report = read_json_report(output)
    assert report.context.generated_seed == MUG
    assert [a.status for a in report.context.seed_attempts] == ["rejected", "accepted"]
    assert report.context.scenarios == (GET,)
    assert len(report.verification.attempts) == 2
    for cells in [
        report.verification.initial_cells,
        *(a.cells for a in report.verification.attempts),
    ]:
        assert cells is not None
        baseline = next(cell for cell in cells if cell.cell == Cell.A0D0)
        got = next(o for o in baseline.outcomes if o.scenario_id == GET.scenario_id)
        assert got.body == {"products": [MUG.fields]}
        head = next(cell for cell in cells if cell.cell == Cell.A1D1)
        got = next(o for o in head.outcomes if o.scenario_id == GET.scenario_id)
        assert got.body == {
            "products": [{"sku": "SEED-MUG", "name": "Mug", "stock": 0, "amount": 5}]
        }
    again = runner.invoke(app, ["resume", run_id, *files])
    assert again.exit_code == 1 and initialize.call_count == 3
