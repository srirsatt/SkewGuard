"""Command-line entry points for deterministic migration verification and reports."""

import sqlite3
from pathlib import Path
from typing import Annotated, NoReturn
from uuid import uuid4

import typer
from langgraph.errors import GraphRecursionError
from pydantic import TypeAdapter

from skewguard.config import RunConfig
from skewguard.exec import db, envs, worktree
from skewguard.exec.probes import ProbeExecutionError
from skewguard.exec.runner import MigrationError
from skewguard.exec.scenarios import Scenario
from skewguard.graph import session
from skewguard.graph.checkpoints import DEFAULT_CHECKPOINT
from skewguard.report import json_report
from skewguard.report.markdown import STATUS_LABELS, render_markdown
from skewguard.static.migrations import MigrationInspectionError

app = typer.Typer(
    help="Reproduce Django migration failures during rolling deploys.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
)

RUN_ERRORS = (
    worktree.GitError, envs.EnvironmentSetupError, db.DatabaseSetupError,
    MigrationError, MigrationInspectionError, ProbeExecutionError,
    GraphRecursionError, sqlite3.Error, OSError, ValueError,
)


def run_verification(
    config: RunConfig,
    *,
    settings_module: str,
    seed_fixture: str | None = None,
    checkpoint_path: Path | None = None,
    run_id: str | None = None,
) -> json_report.RunReport:
    """Own worktree, environment, and database lifetimes for one complete run.

    Prepare base/head environments inside managed_worktrees, installing each
    release's dependencies. Within managed_postgres, invoke the graph with those
    runtime resources and state containing settings and pinned checkout SHAs.
    Context exits must clean up after success or error.
    Return evidence only; the command handles report files and exit status.
    """
    return session.run_session(
        config, settings_module=settings_module, seed_fixture=seed_fixture,
        checkpoint_path=checkpoint_path, run_id=run_id,
        on_progress=lambda message: typer.echo(message, err=True),
    )


def output_paths(json_path: Path, markdown_path: Path | None) -> tuple[Path, Path]:
    """Resolve distinct output destinations before starting expensive execution."""
    json_path = json_path.expanduser().resolve()
    markdown_path = (
        markdown_path.expanduser().resolve()
        if markdown_path is not None
        else json_path.with_suffix(".md")
    )
    if json_path == markdown_path:
        raise typer.BadParameter("JSON and Markdown output paths must be different")
    if json_path.is_dir() or markdown_path.is_dir():
        raise typer.BadParameter("report outputs must be file paths")
    return json_path, markdown_path


def checkpoint_output_path(path: Path, outputs: tuple[Path, Path]) -> Path:
    """Keep report writes from replacing the checkpoint database."""
    resolved = path.expanduser().resolve()
    if resolved in outputs or resolved.is_dir():
        raise typer.BadParameter(
            "checkpoint must be a file distinct from report outputs"
        )
    return resolved


def finish_report(
    report: json_report.RunReport, json_path: Path, markdown_path: Path,
) -> NoReturn:
    """Write the evidence and use the same result status for new and resumed runs."""
    json_report.write_json_report(report, json_path)
    json_report.write_text(markdown_path, render_markdown(report))
    attempts = report.context.scenario_attempts
    if attempts:
        typer.echo(
            f"Generated HTTP scenario: {attempts[-1].status} "
            f"after {len(attempts)} model call(s).", err=True
        )
    typer.echo(STATUS_LABELS[report.summary.status])
    typer.echo(f"JSON: {json_path}")
    typer.echo(f"Markdown: {markdown_path}")
    code = {"no_break_detected": 0, "break_detected": 1, "inconclusive": 3}[
        report.summary.status
    ]
    raise typer.Exit(code=code)


@app.command("run")
def run_command(
    base: Annotated[str, typer.Option("--base", help="Old release Git ref.")],
    head: Annotated[str, typer.Option("--head", help="New release Git ref.")],
    settings: Annotated[
        str,
        typer.Option(
            "--settings",
            envvar="DJANGO_SETTINGS_MODULE",
            help="Django settings module used by both releases.",
        ),
    ],
    repo: Annotated[
        Path, typer.Option("--repo", help="Local Django Git repository.")
    ] = Path("."),
    backend: Annotated[
        Path, typer.Option("--backend", help="Backend path within each release.")
    ] = Path("."),
    seed: Annotated[
        str | None,
        typer.Option("--seed", help="Django fixture to load into the baseline."),
    ] = None,
    json_output: Annotated[
        Path, typer.Option("--json", help="Full JSON evidence report.")
    ] = Path("skewguard-report.json"),
    markdown_output: Annotated[
        Path | None,
        typer.Option("--markdown", help="Markdown report; defaults beside JSON."),
    ] = None,
    checkpoint: Annotated[
        Path, typer.Option("--checkpoint", help="SQLite workflow checkpoint database.")
    ] = DEFAULT_CHECKPOINT,
    timeout: Annotated[
        int, typer.Option("--timeout", min=1, help="Per-operation timeout in seconds.")
    ] = 60,
    probe_row_limit: Annotated[
        int,
        typer.Option(
            "--probe-row-limit", min=1, help="Maximum rows per ORM read probe."
        ),
    ] = 10,
    scenario_model: Annotated[
        str | None, typer.Option(
            "--scenario-model", help="Generate an HTTP check with a local Ollama model."
        )
    ] = None,
    max_llm_calls: Annotated[
        int, typer.Option(
            "--max-llm-calls", min=1, help="Budget for generation (one retry at most)."
        )
    ] = 2,
    scenarios: Annotated[
        Path | None, typer.Option("--scenarios", help="JSON array of HTTP scenarios.")
    ] = None,
) -> None:
    """Execute ORM and supplied HTTP checks with fresh replays for detected failures.

    Exit codes: 0 no break detected, 1 confirmed break, 2 setup/input/output error,
    3 inconclusive. All conclusions apply only to the checks actually executed.
    """
    settings = settings.strip()
    if not settings or not all(part.isidentifier() for part in settings.split(".")):
        raise typer.BadParameter("--settings must be a dotted Python module name")
    if seed is not None:
        seed = seed.strip()
        if not seed:
            raise typer.BadParameter("--seed must not be blank")
    json_path, markdown_path = output_paths(json_output, markdown_output)
    checkpoint_path = checkpoint_output_path(checkpoint, (json_path, markdown_path))
    try:
        if scenarios and scenarios.resolve() in (
            json_path, markdown_path, checkpoint_path
        ):
            raise ValueError("scenario input must differ from outputs and checkpoint")
        config = RunConfig(
            repo_path=repo,
            base_ref=base,
            head_ref=head,
            backend_path=backend,
            timeout_seconds=timeout,
            probe_row_limit=probe_row_limit,
            scenario_model=scenario_model,
            max_llm_calls=max_llm_calls,
            scenarios=(
                TypeAdapter(tuple[Scenario, ...]).validate_json(scenarios.read_bytes())
                if scenarios else ()
            ),
        )
        run_id = uuid4().hex
        typer.echo(f"Run ID: {run_id}", err=True)
        typer.echo(f"Checkpoints: {checkpoint_path}", err=True)
        report = run_verification(
            config, settings_module=settings, seed_fixture=seed,
            checkpoint_path=checkpoint_path, run_id=run_id,
        )
        finish_report(report, json_path, markdown_path)
    except RUN_ERRORS as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error


@app.command("resume")
def resume_command(
    run_id: Annotated[str, typer.Argument(help="Run ID printed by an earlier run.")],
    checkpoint: Annotated[
        Path, typer.Option("--checkpoint", help="SQLite workflow checkpoint database.")
    ] = DEFAULT_CHECKPOINT,
    json_output: Annotated[
        Path, typer.Option("--json")
    ] = Path("skewguard-report.json"),
    markdown_output: Annotated[Path | None, typer.Option("--markdown")] = None,
) -> None:
    """Continue unfinished work at pinned commits or re-render a completed run."""
    json_path, markdown_path = output_paths(json_output, markdown_output)
    checkpoint_path = checkpoint_output_path(checkpoint, (json_path, markdown_path))
    try:
        typer.echo(f"Resuming run: {run_id}", err=True)
        report = session.resume_session(
            checkpoint_path, run_id,
            on_progress=lambda message: typer.echo(message, err=True),
        )
        finish_report(report, json_path, markdown_path)
    except RUN_ERRORS as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error


@app.command("report")
def report_command(
    report_path: Annotated[
        Path, typer.Argument(help="JSON report from an earlier run.")
    ],
    markdown_output: Annotated[
        Path | None,
        typer.Option("--markdown", help="Write Markdown instead of printing it."),
    ] = None,
) -> None:
    """Validate and render saved evidence without starting Django or Docker."""
    if markdown_output is not None and markdown_output.expanduser().resolve() == (
        report_path.expanduser().resolve()
    ):
        raise typer.BadParameter("Markdown output must not overwrite the JSON input")
    try:
        report = json_report.read_json_report(report_path)
        markdown = render_markdown(report)
        if markdown_output is None:
            typer.echo(markdown, nl=False)
        else:
            destination = json_report.write_text(markdown_output, markdown)
            typer.echo(f"Markdown: {destination}")
    except (OSError, ValueError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error
