"""Typed, bounded progress messages and streaming of the verification workflow."""

from __future__ import annotations

# LangGraph's optional hooks expose unbound generics in its public method types.
# pyright: reportUnknownMemberType=false
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, ConfigDict

from skewguard.graph.runtime import RunResources
from skewguard.graph.state import RunState

if TYPE_CHECKING:
    from skewguard.graph.build import VerificationGraph

type NodeName = Literal["static_analyze", "execute_matrix", "report"]
type ProgressCallback = Callable[[str], None]

NODE_LABELS: dict[str, str] = {
    "static_analyze": "static analysis",
    "execute_matrix": "matrix and reproduction checks",
    "report": "report generation",
}


class ProgressEvent(BaseModel):
    """Only scripted phase identifiers are rendered from custom stream events."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["skewguard_progress"] = "skewguard_progress"
    node: NodeName


def stream_workflow(
    workflow: VerificationGraph,
    initial: RunState | None,
    resources: RunResources,
    *,
    config: RunnableConfig,
    durable: bool,
    on_progress: ProgressCallback | None = None,
) -> RunState:
    """Consume v2 typed streams, retaining state while rendering only phase labels."""
    final: RunState | None = None
    for part in workflow.stream(
        initial,
        config=config,
        context=resources,
        stream_mode=["custom", "updates", "values"],
        version="v2",
        durability="sync" if durable else None,
    ):
        if part["type"] == "values":
            final = RunState.model_validate(part["data"])
        elif part["type"] == "updates" and on_progress is not None:
            for node in part["data"]:
                if node in NODE_LABELS:
                    on_progress(f"Completed: {NODE_LABELS[node]}")
        elif part["type"] == "custom" and on_progress is not None:
            payload = part["data"]
            if (
                isinstance(payload, dict)
                and payload.get("kind") == "skewguard_progress"
            ):
                event = ProgressEvent.model_validate(payload)
                on_progress(f"Starting: {NODE_LABELS[event.node]}")
    if final is None:
        raise ValueError("verification graph returned no state")
    return final
