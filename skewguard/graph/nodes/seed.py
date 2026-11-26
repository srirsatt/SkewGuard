"""Ground a proposed seed in an old GET, then validate the insert on a private clone."""

from typing import Literal, TypedDict

from langgraph.runtime import Runtime
from pydantic import ValidationError

from skewguard.agents.generation import generate_reply, source_evidence
from skewguard.agents.scenario import ScenarioProposal
from skewguard.agents.seed import SEED_SYSTEM_PROMPT, SeedAttempt, SeedDraft
from skewguard.config import RunConfig
from skewguard.exec.db import temporary_clone
from skewguard.exec.envs import backend_directory
from skewguard.exec.probes import insert_seed_proposal
from skewguard.exec.scenarios import Scenario
from skewguard.graph.progress import ProgressEvent
from skewguard.graph.runtime import RunResources, validate_resources
from skewguard.graph.state import RunState


class SeedDraftUpdate(TypedDict):
    seed_draft: SeedDraft


class SeedValidationUpdate(TypedDict, total=False):
    config: RunConfig
    seed_attempts: tuple[SeedAttempt, ...]


def seed_target(state: RunState) -> Scenario | None:
    for scenario in state.config.scenarios:
        if scenario.method == "GET":
            return scenario
    if state.scenario_pending:
        try:
            proposed = ScenarioProposal.model_validate_json(
                state.proposal_reply
            ).scenario
        except ValidationError:
            return None
        if proposed is not None and proposed.method == "GET":
            return proposed
    return None


def should_start_seed(state: RunState) -> bool:
    return (
        state.config.seed_model is not None
        and not state.seed_attempts
        and state.remaining_model_calls > 0
        and seed_target(state) is not None
    )


def propose_seed(state: RunState, runtime: Runtime[RunResources]) -> SeedDraftUpdate:
    resources = validate_resources(state, runtime.context)
    model = state.config.seed_model
    target = seed_target(state)
    if not model or target is None or state.static_context is None:
        raise ValueError("seed generation requires a model, GET, and static context")
    if state.remaining_model_calls <= 0:
        raise ValueError("seed generation exhausted the model call budget")
    runtime.stream_writer(ProgressEvent(node="propose_seed").model_dump())
    checkout = resources.base.checkout
    evidence = source_evidence(
        state.static_context,
        checkout,
        backend_directory(state.config, checkout),
        seed_for=target,
    )
    reply = generate_reply(
        model,
        state.config.timeout_seconds,
        evidence,
        system_prompt=SEED_SYSTEM_PROMPT,
        previous=state.seed_draft.reply if state.seed_draft else "",
        feedback=state.seed_attempts[-1].detail if state.seed_attempts else "",
    )
    return {"seed_draft": SeedDraft.parse_reply(target.scenario_id, reply)}


def validate_seed(
    state: RunState,
    runtime: Runtime[RunResources],
) -> SeedValidationUpdate:
    resources = validate_resources(state, runtime.context)
    draft = state.seed_draft
    if draft is None:
        raise ValueError("seed validation requires a saved proposal")
    runtime.stream_writer(ProgressEvent(node="validate_seed").model_dump())
    update: SeedValidationUpdate = {}
    proposal = draft.proposal
    detail = draft.parse_error
    if proposal is not None:
        with temporary_clone(state.config, resources.scenario_snapshot(state)) as db:
            result = insert_seed_proposal(
                state.config,
                resources.base,
                db,
                settings_module=state.settings_module,
                proposal=proposal,
            )
        if result.status == "ok":
            update["config"] = state.config.model_copy(
                update={"generated_seed": proposal}
            )
            detail = f"Django inserted one {proposal.model} row on A0D0."
        else:
            detail = f"{result.exception_type}: {result.exception_message}"[:800]
    if not detail:
        raise ValueError("seed draft has neither an insert result nor parser feedback")
    attempt = SeedAttempt(
        scenario_id=draft.scenario_id,
        proposal=proposal,
        detail=detail,
        status="accepted" if "config" in update else "rejected",
    )
    update["seed_attempts"] = (*state.seed_attempts, attempt)
    return update


def after_seed(
    state: RunState,
) -> Literal["propose_seed", "qualify_proposal", "execute_matrix"]:
    if (
        state.seed_attempts[-1].status == "rejected"
        and len(state.seed_attempts) < 4
        and state.remaining_model_calls > 0
    ):
        return "propose_seed"
    if state.scenario_pending:
        return "qualify_proposal"
    return "execute_matrix"
