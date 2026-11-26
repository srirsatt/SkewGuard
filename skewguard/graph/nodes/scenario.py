"""Propose an HTTP check, execute it on A0D0 and route rejected proposals."""

from typing import Literal, TypedDict

from langgraph.runtime import Runtime
from pydantic import ValidationError

from skewguard.agents.generation import generate_reply, source_evidence
from skewguard.agents.scenario import ScenarioAttempt, ScenarioProposal
from skewguard.config import RunConfig
from skewguard.exec.envs import backend_directory
from skewguard.exec.qualification import qualify_scenario
from skewguard.exec.scenarios import Scenario
from skewguard.exec.types import ComparisonIssue
from skewguard.graph.nodes.seed import should_start_seed
from skewguard.graph.progress import ProgressEvent
from skewguard.graph.runtime import RunResources, validate_resources
from skewguard.graph.state import RunState


class ReplyUpdate(TypedDict):
    proposal_reply: str
    scenario_pending: bool


class QualificationUpdate(TypedDict, total=False):
    scenario_pending: bool
    scenario_attempts: tuple[ScenarioAttempt, ...]
    config: RunConfig


def after_analysis(
    state: RunState,
) -> Literal["propose_scenario", "propose_seed", "execute_matrix"]:
    if state.config.scenario_model:
        return "propose_scenario"
    if should_start_seed(state):
        return "propose_seed"
    return "execute_matrix"


def after_proposal(state: RunState) -> Literal["propose_seed", "qualify_proposal"]:
    return "propose_seed" if should_start_seed(state) else "qualify_proposal"


def propose_scenario(state: RunState, runtime: Runtime[RunResources]) -> ReplyUpdate:
    resources = validate_resources(state, runtime.context)
    model = state.config.scenario_model
    if not model or state.static_context is None:
        raise ValueError("scenario generation requires a model and static context")
    runtime.stream_writer(ProgressEvent(node="propose_scenario").model_dump())
    checkout = resources.base.checkout
    evidence = source_evidence(
        state.static_context, checkout, backend_directory(state.config, checkout)
    )
    reply = generate_reply(
        model,
        state.config.timeout_seconds,
        evidence,
        previous=state.proposal_reply,
        feedback=state.scenario_attempts[-1].detail if state.scenario_attempts else "",
    )
    return {"proposal_reply": reply, "scenario_pending": True}


def check_baseline(
    state: RunState,
    resources: RunResources,
    scenario: Scenario,
) -> ScenarioAttempt:
    if any(item.scenario_id == scenario.scenario_id for item in state.config.scenarios):
        return ScenarioAttempt(
            status="rejected",
            scenario=scenario,
            detail=f"Scenario ID {scenario.scenario_id} is already in the plan.",
        )
    observed = qualify_scenario(
        state.config,
        resources.base,
        resources.scenario_snapshot(state),
        settings_module=state.settings_module,
        scenario=scenario,
    )
    if isinstance(observed, ComparisonIssue):
        detail = observed.message[:500]
        outcome = observed.outcome
        if outcome is not None and outcome.http_status is not None:
            detail = f"A0D0 returned HTTP {outcome.http_status}. {detail}"
            if isinstance(outcome.body, dict):
                keys = ", ".join(sorted(outcome.body)[:40])
                detail += f". Actual top-level keys: {keys[:500]}."
        return ScenarioAttempt(status="rejected", scenario=scenario, detail=detail)
    return ScenarioAttempt(
        status="accepted",
        scenario=scenario,
        detail=f"Qualified on A0D0 with HTTP {observed.http_status}.",
    )


def qualify_proposal(
    state: RunState,
    runtime: Runtime[RunResources],
) -> QualificationUpdate:
    resources = validate_resources(state, runtime.context)
    runtime.stream_writer(ProgressEvent(node="qualify_proposal").model_dump())
    try:
        proposal = ScenarioProposal.model_validate_json(state.proposal_reply)
    except ValidationError as error:
        issues = error.errors(include_input=False, include_url=False)
        attempt = ScenarioAttempt(
            status="rejected", detail=f"Invalid proposal: {issues}"[:800]
        )
    else:
        if proposal.scenario is None:
            attempt = ScenarioAttempt(status="skipped", detail=proposal.reason)
        else:
            attempt = check_baseline(state, resources, proposal.scenario)
    update: QualificationUpdate = {
        "scenario_pending": False,
        "scenario_attempts": (*state.scenario_attempts, attempt)
    }
    if attempt.status == "accepted" and attempt.scenario is not None:
        update["config"] = state.config.model_copy(
            update={"scenarios": (*state.config.scenarios, attempt.scenario)}
        )
    return update


def after_qualification(
    state: RunState,
) -> Literal["propose_scenario", "execute_matrix"]:
    rejected = state.scenario_attempts[-1].status == "rejected"
    can_retry = len(state.scenario_attempts) < 2 and state.remaining_model_calls > 0
    if rejected and can_retry:
        return "propose_scenario"
    return "execute_matrix"
