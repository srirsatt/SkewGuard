"""Exercise proposal routing, real A0D0 feedback, and checkpoint recovery."""

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
from skewguard.cli import app
from skewguard.exec.scenarios import Scenario
from skewguard.exec.types import Outcome
from skewguard.graph import runtime as graph_runtime
from skewguard.graph.nodes import scenario as node
from skewguard.graph.runtime import RunResources
from skewguard.graph.state import RunState
from skewguard.report.json_report import read_json_report

REQUEST = Scenario(
    scenario_id="http:create-product",
    method="POST",
    path="/products/",
    kind="write",
    body={"sku": "REPAIR", "name": "Mug", "price": 12, "stock": 1},
    compare_fields=["price"],
)


def answer(request: Scenario | None) -> str:
    return ScenarioProposal(
        scenario=request, reason="Old shop endpoint."
    ).model_dump_json()


@pytest.mark.parametrize(
    ("raw", "budget", "expected", "next_node"),
    [
        ("broken json", 2, "rejected", "propose_scenario"),
        ("{}", 1, "rejected", "execute_matrix"),
        (answer(None), 2, "skipped", "execute_matrix"),
        (answer(REQUEST), 2, "rejected", "propose_scenario"),
    ],
)
def test_invalid_null_and_duplicate_proposals(
    graph_inputs: tuple[RunState, RunResources],
    raw: str,
    budget: int,
    expected: str,
    next_node: str,
) -> None:
    state, resources = graph_inputs
    config = state.config.model_copy(
        update={"max_llm_calls": budget, "scenarios": (REQUEST,)}
    )
    state = state.model_copy(update={"config": config, "proposal_reply": raw})
    updated = state.model_copy(
        update=dict(node.qualify_proposal(state, Runtime(context=resources)))
    )
    attempt = updated.scenario_attempts[-1]
    assert attempt.status == expected
    assert node.after_qualification(updated) == next_node
    assert updated.config.scenarios == (REQUEST,)
    if expected == "rejected":
        exhausted = updated.model_copy(update={"scenario_attempts": (attempt, attempt)})
        assert node.after_qualification(exhausted) == "execute_matrix"


def test_first_qualified_proposal_extends_manual_plan_and_stops(
    graph_inputs: tuple[RunState, RunResources],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, resources = graph_inputs
    manual = REQUEST.model_copy(update={"scenario_id": "http:manual"})
    config = state.config.model_copy(update={"scenarios": (manual,)})
    state = state.model_copy(
        update={"config": config, "proposal_reply": answer(REQUEST)}
    )
    prepare = Mock(return_value=resources.server)
    monkeypatch.setattr(graph_runtime, "prepare_old_snapshot", prepare)
    monkeypatch.setattr(
        node,
        "qualify_scenario",
        Mock(
            return_value=Outcome(
                scenario_id=REQUEST.scenario_id,
                status="http_status",
                http_status=201,
            )
        ),
    )
    update = node.qualify_proposal(state, Runtime(context=resources))
    updated = state.model_copy(update=dict(update))
    assert node.after_qualification(updated) == "execute_matrix"
    assert updated.config.scenarios == (manual, REQUEST)
    assert updated.scenario_attempts[-1].status == "accepted"
    assert resources.scenario_snapshot(state) == resources.server
    prepare.assert_called_once()


@pytest.mark.skipif(
    os.environ.get("SKEWGUARD_TEST_POSTGRES") != "1",
    reason="set SKEWGUARD_TEST_POSTGRES=1 for Docker",
)
def test_cli_repairs_a_rejected_post_then_resumes_without_new_model_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_demo_repo(tmp_path / "shop")
    output = tmp_path / "report.json"
    checkpoint = tmp_path / "run.sqlite"
    wrong = REQUEST.model_copy(update={"compare_fields": ["amount"]})
    replies = FakeListChatModel(responses=[answer(wrong), answer(REQUEST)])
    init = Mock(return_value=replies)
    monkeypatch.setattr(generation, "init_chat_model", init)
    # Interrupt the first qualification after LangGraph has saved the model reply.
    qualify = Mock(side_effect=RuntimeError("interrupted before HTTP execution"))
    original = node.qualify_scenario
    monkeypatch.setattr(node, "qualify_scenario", qualify)
    runner = CliRunner()
    files = ["--checkpoint", str(checkpoint), "--json", str(output)]
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
            "--scenario-model",
            "fixture-model",
            *files,
        ],
    )
    assert isinstance(first.exception, RuntimeError), first.output
    assert str(first.exception) == "interrupted before HTTP execution"
    assert init.call_count == 1
    run_id = next(
        s.removeprefix("Run ID: ")
        for s in first.output.splitlines()
        if s.startswith("Run ID: ")
    )
    monkeypatch.setattr(node, "qualify_scenario", original)
    generate = Mock(wraps=node.generate_reply)
    monkeypatch.setattr(node, "generate_reply", generate)
    result = runner.invoke(app, ["resume", run_id, *files])
    assert result.exit_code == 1 and output.exists(), (result.output, result.exception)
    assert init.call_count == 2
    generate.assert_called_once()
    assert generate.call_args.kwargs["previous"] == answer(wrong)
    feedback = generate.call_args.kwargs["feedback"]
    assert "baseline response lacks keys: amount" in feedback
    assert "HTTP 201" in feedback and "price" in feedback
    report = read_json_report(output)
    assert [a.status for a in report.context.scenario_attempts] == [
        "rejected",
        "accepted",
    ]
    assert report.context.scenarios == (REQUEST,)
    assert report.verification.initial_cells[0].qualified_scenarios == (REQUEST,)
    assert report.summary.status == "break_detected"
    rerender = runner.invoke(app, ["resume", run_id, *files])
    assert rerender.exit_code == 1 and init.call_count == 2
