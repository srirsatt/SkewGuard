"""Command-line entry points for deterministic migration verification and reports."""

from pathlib import Path
from typing import Annotated

import typer

from skewguard.config import RunConfig
from skewguard.exec import db, envs, worktree, verify
from skewguard.exec.probes import ProbeExecutionError
from skewguard.exec.runner import MigrationError
from skewguard.report import json_report
from skewguard.report.markdown import STATUS_LABELS, render_markdown

app = typer.Typer(
    help="Reproduce Django migration failures during rolling deploys.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
)


def run_verification(
    config: RunConfig,
    *,
    settings_module: str,
    seed_fixture: str | None = None,
) -> json_report.RunReport:
    """Own worktree, environment, and database lifetimes for one complete run.

    Prepare base/head environments inside managed_worktrees, installing each
    release's dependencies. Within managed_postgres, call verify_matrix with
    those environments and the settings/fixture inputs. Build the report using
    the pinned checkout SHAs. Context exits must clean up after success or error.
    Return evidence only; the command handles report files and exit status.
    """
    with worktree.managed_worktrees(config) as checkouts:
        typer.echo("preparing base environment...", err=True)
        base = envs.create_environment(config, checkouts.base)
        envs.install_dependencies(config, base)

        typer.echo("preparing head environment...", err=True)
        head = envs.create_environment(config, checkouts.head)
        envs.install_dependencies(config, head)

        typer.echo("running matrix and reproduction checks", err=True)
        with db.managed_postgres(config) as server:
            result = verify.verify_matrix(
                config, base, head, server, settings_module=settings_module, seed_fixture=seed_fixture
            )

        return json_report.build_report(
            config, checkouts, result, settings_module=settings_module, seed_fixture=seed_fixture,
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
    timeout: Annotated[
        int, typer.Option("--timeout", min=1, help="Per-operation timeout in seconds.")
    ] = 60,
    probe_row_limit: Annotated[
        int,
        typer.Option(
            "--probe-row-limit", min=1, help="Maximum rows per ORM read probe."
        ),
    ] = 10,
) -> None:
    """Execute ORM checks and two fresh replays for any detected failures.

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
    try:
        config = RunConfig(
            repo_path=repo,
            base_ref=base,
            head_ref=head,
            backend_path=backend,
            timeout_seconds=timeout,
            probe_row_limit=probe_row_limit,
        )
        report = run_verification(config, settings_module=settings, seed_fixture=seed)
        json_report.write_json_report(report, json_path)
        json_report.write_text(markdown_path, render_markdown(report))
    except (
        worktree.GitError,
        envs.EnvironmentSetupError,
        db.DatabaseSetupError,
        MigrationError,
        ProbeExecutionError,
        OSError,
        ValueError,
    ) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error
    typer.echo(STATUS_LABELS[report.summary.status])
    typer.echo(f"JSON: {json_path}")
    typer.echo(f"Markdown: {markdown_path}")
    code = {"no_break_detected": 0, "break_detected": 1, "inconclusive": 3}[
        report.summary.status
    ]
    raise typer.Exit(code=code)


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
