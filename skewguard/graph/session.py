"""Own transient resources while starting or resuming a durable verification run."""

# LangGraph's optional hooks expose unbound generics in its public method types.
# pyright: reportUnknownMemberType=false

from collections.abc import Generator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from uuid import uuid4

from skewguard.config import RunConfig
from skewguard.exec import db, envs, worktree
from skewguard.graph.build import build_graph
from skewguard.graph.checkpoints import CheckpointError, open_checkpoints, run_config
from skewguard.graph.progress import ProgressCallback, stream_workflow
from skewguard.graph.runtime import RunResources
from skewguard.graph.state import RunState
from skewguard.report.json_report import RunReport


@contextmanager
def prepared_resources(
    config: RunConfig,
    *,
    pins: tuple[str, str] | None = None,
    on_progress: ProgressCallback | None = None,
) -> Generator[RunResources, None, None]:
    """Rebuild resources from recorded SHAs on resume; preserve input provenance."""
    checkout_config = (
        config
        if pins is None
        else RunConfig.model_validate(
            {
                **config.model_dump(),
                "base_ref": pins[0],
                "head_ref": pins[1],
            }
        )
    )
    with worktree.managed_worktrees(checkout_config) as checkouts:
        if on_progress is not None:
            on_progress("Preparing base environment")
        base = envs.create_environment(config, checkouts.base)
        envs.install_dependencies(config, base)
        if on_progress is not None:
            on_progress("Preparing head environment")
        head = envs.create_environment(config, checkouts.head)
        envs.install_dependencies(config, head)
        if on_progress is not None:
            on_progress("Starting disposable PostgreSQL")
        with db.managed_postgres(config) as server:
            yield RunResources(checkouts=checkouts, base=base, head=head, server=server)


def completed_report(state: RunState) -> RunReport:
    """Require evidence and provenance to agree before reusing a saved report."""
    report = state.report
    if report is None:
        raise CheckpointError("verification graph completed without a report")
    if report.verification != state.verification:
        raise CheckpointError("saved report conflicts with graph verification evidence")
    expected = {
        "repo_path": state.config.repo_path,
        "backend_path": state.config.backend_path,
        "base_ref": state.config.base_ref,
        "head_ref": state.config.head_ref,
        "base_sha": state.base_sha,
        "head_sha": state.head_sha,
        "settings_module": state.settings_module,
        "seed_fixture": state.seed_fixture,
        "timeout_seconds": state.config.timeout_seconds,
        "probe_row_limit": state.config.probe_row_limit,
        "scenarios": state.config.scenarios,
        "scenario_model": state.config.scenario_model,
        "scenario_attempts": state.scenario_attempts,
    }
    if any(getattr(report.context, name) != value for name, value in expected.items()):
        raise CheckpointError("saved report conflicts with graph run inputs")
    return report


def run_session(
    config: RunConfig,
    *,
    settings_module: str,
    seed_fixture: str | None = None,
    checkpoint_path: Path | None = None,
    run_id: str | None = None,
    on_progress: ProgressCallback | None = None,
) -> RunReport:
    """Start a fresh thread, preserving completed phases if graph execution fails."""
    selected_id = run_id or uuid4().hex
    selected_config = run_config(selected_id)
    storage = (
        open_checkpoints(checkpoint_path, selected_id, create=True)
        if checkpoint_path is not None
        else nullcontext(None)
    )
    with storage as saver:
        if saver is not None and saver.get_tuple(selected_config) is not None:
            raise CheckpointError("run ID already exists; use resume")
        workflow = build_graph(saver)
        with prepared_resources(config, on_progress=on_progress) as resources:
            initial = RunState(
                config=config,
                settings_module=settings_module,
                seed_fixture=seed_fixture,
                base_sha=resources.checkouts.base_sha,
                head_sha=resources.checkouts.head_sha,
            )
            final = stream_workflow(
                workflow,
                initial,
                resources,
                config=selected_config,
                durable=saver is not None,
                on_progress=on_progress,
            )
            return completed_report(final)


def resume_session(
    checkpoint_path: Path,
    run_id: str,
    *,
    on_progress: ProgressCallback | None = None,
) -> RunReport:
    """Resume unfinished nodes with fresh resources or return a completed report."""
    selected_config = run_config(run_id)
    with open_checkpoints(checkpoint_path, run_id, create=False) as saver:
        workflow = build_graph(saver)
        snapshot = workflow.get_state(selected_config)
        if not snapshot.values:
            raise CheckpointError("run ID was not found in this checkpoint database")
        state = RunState.model_validate(snapshot.values)
        if not snapshot.next:
            return completed_report(state)
        if any(task.interrupts for task in snapshot.tasks):
            raise CheckpointError(
                "run requires human input; interactive resume is not available"
            )
        with prepared_resources(
            state.config,
            pins=(state.base_sha, state.head_sha),
            on_progress=on_progress,
        ) as resources:
            final = stream_workflow(
                workflow,
                None,
                resources,
                config=selected_config,
                durable=True,
                on_progress=on_progress,
            )
            return completed_report(final)
