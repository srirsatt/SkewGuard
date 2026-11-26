"""Construct one local model turn using old-release evidence and checker feedback."""

import json
from pathlib import Path

import httpx
from langchain.agents import create_agent  # pyright: ignore[reportUnknownVariableType]
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain.agents.middleware.types import InputAgentState
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, HumanMessage
from ollama import ResponseError

from skewguard.agents.scenario import SCENARIO_SYSTEM_PROMPT, ScenarioProposal
from skewguard.static.code_refs import SKIP_DIRECTORIES
from skewguard.static.types import StaticContext


def source_evidence(context: StaticContext, checkout: Path, backend: Path) -> str:
    """Supply migration metadata, URL configs and affected old application files."""
    paths: list[Path] = []
    for directory, dirs, files in backend.walk():
        dirs[:] = sorted(name for name in dirs if name not in SKIP_DIRECTORIES)
        if "urls.py" in files:
            paths.append(directory / "urls.py")
    referenced = {checkout / ref.source.path for ref in context.base_code.references}
    paths.extend(sorted(referenced, key=lambda p: (p.name == "models.py", p)))
    sources: dict[str, str] = {}
    remaining = 18000
    for path in dict.fromkeys(paths):
        if remaining <= 0:
            break
        if not path.resolve().is_relative_to(backend) or path.is_symlink():
            continue
        try:
            with path.open(encoding="utf-8", errors="replace") as file:
                excerpt = file.read(min(4500, remaining))
        except OSError:
            continue
        sources[path.relative_to(checkout).as_posix()] = excerpt
        remaining -= len(excerpt)
    # Keep whole operation descriptions so the last item remains readable.
    changes: list[str] = []
    budget = 6000
    for migration in context.migrations.new_migrations:
        for operation in migration.operations:
            description = operation.model_dump_json(
                exclude_none=True, exclude={"source", "details"}
            )
            line = f"{migration.key.app_label}.{migration.key.name}: {description}"
            if len(line) <= budget:
                changes.append(line)
                budget -= len(line)
    return json.dumps(
        {
            "migration_changes": changes,
            "old_release_source": sources,
            "response_json_schema": ScenarioProposal.model_json_schema(),
        }
    )


def generate_reply(
    model_name: str,
    timeout: int,
    evidence: str,
    *,
    previous: str,
    feedback: str,
) -> str:
    """Perform exactly one model turn. The outer graph owns repair decisions."""
    model = init_chat_model(
        model_name,
        model_provider="ollama",
        base_url="http://127.0.0.1:11434",
        format="json",
        temperature=0,
        seed=7,
        num_ctx=8192,
        num_predict=700,
        client_kwargs={"timeout": timeout},
    )
    agent = create_agent(
        model,
        tools=[],
        system_prompt=SCENARIO_SYSTEM_PROMPT,
        middleware=[ModelCallLimitMiddleware(run_limit=1, exit_behavior="error")],
    )
    inputs: InputAgentState = {"messages": [HumanMessage(content=evidence)]}
    if feedback:
        inputs["messages"].extend(
            [
                AIMessage(content=previous),
                HumanMessage(
                    content=f"{feedback}\nRevise the proposal in the same JSON format."
                ),
            ]
        )
    try:
        result = agent.invoke(  # pyright: ignore[reportUnknownMemberType]
            inputs,
            version="v2",
        ).value
    except (ConnectionError, httpx.HTTPError, ResponseError) as error:
        raise ValueError(f"Ollama could not generate a scenario: {error}") from error
    return result["messages"][-1].text
