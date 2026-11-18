"""Check CLI argument validation, status codes, report I/O, and resource ownership."""

import re
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

import skewguard.cli as cli
from skewguard.config import RunConfig
from skewguard.exec.db import DatabaseConnection
from skewguard.exec.envs import EnvironmentSetupError, PythonEnvironment
from skewguard.exec.probes import ProbeExecutionError
from skewguard.exec.worktree import CheckoutPair
from skewguard.graph.build import build_graph
from skewguard.graph.checkpoints import open_checkpoints, run_config
from skewguard.graph.runtime import RunResources
from skewguard.graph.state import RunState
from skewguard.report.json_report import RunReport, read_json_report, write_json_report
from skewguard.report.markdown import render_markdown
from skewguard.static.migrations import MigrationInspectionError

runner = CliRunner()


def arguments(tmp_path: Path) -> list[str]:
    return [
        "run",
        "--repo",
        str(tmp_path),
        "--base",
        "release-v1",
        "--head",
        "release-v2",
        "--settings",
        "shop.settings",
        "--seed",
        "products",
        "--json",
        str(tmp_path / "out.json"),
        "--checkpoint",
        str(tmp_path / "checkpoints.sqlite"),
    ]


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("safe", 0),
        ("breaking", 1),
        ("flaky", 3),
        ("inconclusive", 3),
        ("head_failure", 3),
    ],
)
def test_run_writes_both_reports_and_uses_evidence_exit_codes(
    report_factory: Callable[[str], RunReport],
    mode: str,
    code: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = report_factory(mode)
    execute = Mock(return_value=report)
    monkeypatch.setattr(cli, "run_verification", execute)

    result = runner.invoke(cli.app, arguments(tmp_path))

    assert result.exit_code == code, result.output
    assert read_json_report(tmp_path / "out.json") == report
    assert (tmp_path / "out.md").read_text(encoding="utf-8") == render_markdown(report)
    config = execute.call_args.args[0]
    assert isinstance(config, RunConfig)
    assert config.repo_path == tmp_path
    assert execute.call_args.kwargs["settings_module"] == "shop.settings"
    assert execute.call_args.kwargs["seed_fixture"] == "products"
    assert execute.call_args.kwargs["checkpoint_path"] == (
        tmp_path / "checkpoints.sqlite"
    )
    identifier = execute.call_args.kwargs["run_id"]
    assert re.fullmatch(r"[0-9a-f]{32}", identifier)
    assert f"Run ID: {identifier}" in result.output


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--base", " "),
        ("--settings", "bad-name"),
        ("--seed", " "),
        ("--backend", "../outside"),
        ("--timeout", "0"),
        ("--probe-row-limit", "0"),
    ],
)
def test_invalid_inputs_fail_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    value: str,
) -> None:
    execute = Mock()
    monkeypatch.setattr(cli, "run_verification", execute)
    args = arguments(tmp_path)
    if flag in args:
        args[args.index(flag) + 1] = value
    else:
        args.extend([flag, value])

    result = runner.invoke(cli.app, args)

    assert result.exit_code == 2
    execute.assert_not_called()
    assert not (tmp_path / "out.json").exists()


def test_identical_output_paths_are_rejected_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execute = Mock()
    monkeypatch.setattr(cli, "run_verification", execute)

    result = runner.invoke(
        cli.app, [*arguments(tmp_path), "--markdown", str(tmp_path / "out.json")]
    )

    assert result.exit_code == 2
    execute.assert_not_called()


def test_settings_can_come_from_the_django_environment_variable(
    report_factory: Callable[[str], RunReport],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execute = Mock(return_value=report_factory("safe"))
    monkeypatch.setattr(cli, "run_verification", execute)
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "shop.settings")
    args = arguments(tmp_path)
    start = args.index("--settings")
    del args[start : start + 2]

    assert runner.invoke(cli.app, args).exit_code == 0
    assert execute.call_args.kwargs["settings_module"] == "shop.settings"


@pytest.mark.parametrize("error_type", [ProbeExecutionError, MigrationInspectionError])
def test_execution_errors_have_a_distinct_exit_code_and_do_not_create_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[RuntimeError],
) -> None:
    monkeypatch.setattr(
        cli,
        "run_verification",
        Mock(side_effect=error_type("worker unavailable")),
    )

    result = runner.invoke(cli.app, arguments(tmp_path))

    assert result.exit_code == 2
    assert "worker unavailable" in result.output
    assert not (tmp_path / "out.json").exists()
    assert not (tmp_path / "out.md").exists()


def test_report_command_renders_saved_evidence_without_execution(
    report_factory: Callable[[str], RunReport],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = report_factory("breaking")
    path = write_json_report(report, tmp_path / "saved.json")
    execute = Mock(side_effect=AssertionError("must not execute"))
    monkeypatch.setattr(cli, "run_verification", execute)

    result = runner.invoke(cli.app, ["report", str(path)])

    assert result.exit_code == 0
    assert result.output == render_markdown(report)
    execute.assert_not_called()


def test_report_command_rejects_invalid_json_and_input_overwrite(
    tmp_path: Path,
) -> None:
    path = tmp_path / "invalid.json"
    path.write_text("not JSON", encoding="utf-8")

    assert runner.invoke(cli.app, ["report", str(path)]).exit_code == 2
    assert (
        runner.invoke(cli.app, ["report", str(path), "--markdown", str(path)]).exit_code
        == 2
    )
    assert path.read_text(encoding="utf-8") == "not JSON"


@pytest.mark.parametrize(
    "failure", [None, "install", "inspect", "execute", "missing_report"]
)
def test_orchestration_owns_both_environments_and_cleans_resources_after_errors(
    report_factory: Callable[[str], RunReport],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
) -> None:
    sample = report_factory("breaking")
    config = RunConfig(repo_path=tmp_path, base_ref="release-v1", head_ref="release-v2")
    checkouts = CheckoutPair(
        base=tmp_path / "base",
        head=tmp_path / "head",
        base_sha="a" * 40,
        head_sha="b" * 40,
    )
    environments = [
        PythonEnvironment(
            checkout=path,
            venv=path / ".skewguard-venv",
            python=path / ".skewguard-venv/bin/python",
        )
        for path in (checkouts.base, checkouts.head)
    ]
    server = DatabaseConnection(
        host="localhost",
        port=5432,
        username="test",
        password=SecretStr("test-secret"),
        dbname="postgres",
    )
    worktrees = MagicMock()
    worktrees.__enter__.return_value = checkouts
    postgres = MagicMock()
    postgres.__enter__.return_value = server
    start_database = Mock(return_value=postgres)
    create = Mock(side_effect=environments)
    install = Mock(
        side_effect=EnvironmentSetupError("install failed")
        if failure == "install"
        else None
    )
    execute = Mock(
        side_effect=(
            ProbeExecutionError("execution failed") if failure == "execute"
            else MigrationInspectionError("inspection failed") if failure == "inspect"
            else None
        ),
        return_value=RunState(
                config=config,
                settings_module="shop.settings",
                base_sha=checkouts.base_sha,
                head_sha=checkouts.head_sha,
                seed_fixture="products",
                verification=sample.verification,
                report=None if failure == "missing_report" else sample,
        ),
    )
    monkeypatch.setattr(cli.worktree, "managed_worktrees", Mock(return_value=worktrees))
    monkeypatch.setattr(cli.db, "managed_postgres", start_database)
    monkeypatch.setattr(cli.envs, "create_environment", create)
    monkeypatch.setattr(cli.envs, "install_dependencies", install)
    graph = Mock()
    monkeypatch.setattr(cli.session, "build_graph", Mock(return_value=graph))
    monkeypatch.setattr(cli.session, "stream_workflow", execute)

    if failure is not None:
        with pytest.raises(
            (
                EnvironmentSetupError,
                ProbeExecutionError,
                MigrationInspectionError,
                ValueError,
            )
        ):
            cli.run_verification(
                config, settings_module="shop.settings", seed_fixture="products"
            )
    else:
        report = cli.run_verification(
            config, settings_module="shop.settings", seed_fixture="products"
        )
        assert report == sample
        assert [call.args for call in create.call_args_list] == [
            (config, checkouts.base),
            (config, checkouts.head),
        ]
        assert [call.args for call in install.call_args_list] == [
            (config, environment) for environment in environments
        ]
        assert execute.call_args.args[0] is graph
        initial = execute.call_args.args[1]
        assert isinstance(initial, RunState)
        assert initial.config == config
        assert initial.base_sha == checkouts.base_sha
        assert initial.head_sha == checkouts.head_sha
        assert initial.settings_module == "shop.settings"
        assert initial.seed_fixture == "products"
        assert initial.verification is None
        assert initial.report is None
        resources = execute.call_args.args[2]
        assert isinstance(resources, RunResources)
        assert resources.checkouts == checkouts
        assert resources.base == environments[0]
        assert resources.head == environments[1]
        assert resources.server == server
        assert execute.call_args.kwargs["durable"] is False
    worktrees.__exit__.assert_called_once()
    if failure == "install":
        start_database.assert_not_called()
    else:
        postgres.__exit__.assert_called_once()


@pytest.mark.parametrize(("mode", "exit_code"), [("safe", 0), ("breaking", 1)])
def test_resume_completed_run_writes_saved_evidence_without_preparing_resources(
    report_factory: Callable[[str], RunReport], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, mode: str, exit_code: int,
) -> None:
    sample = report_factory(mode)
    context = sample.context
    state = RunState(
        config=RunConfig(repo_path=context.repo_path, base_ref=context.base_ref,
                         head_ref=context.head_ref),
        settings_module=context.settings_module, base_sha=context.base_sha,
        head_sha=context.head_sha, seed_fixture=context.seed_fixture,
        verification=sample.verification, report=sample,
    )
    path = tmp_path / "checkpoints.sqlite"
    identifier = "1" * 32
    with open_checkpoints(path, identifier, create=True) as saver:
        build_graph(saver).update_state(  # pyright: ignore[reportUnknownMemberType]
            run_config(identifier), state.model_dump(), as_node="report",
        )
    prepare = Mock(side_effect=AssertionError("must not prepare"))
    monkeypatch.setattr(cli.session, "prepared_resources", prepare)
    output = tmp_path / "resumed.json"
    result = runner.invoke(cli.app, ["resume", identifier, "--checkpoint", str(path),
                                    "--json", str(output)])
    assert result.exit_code == exit_code, result.output
    assert read_json_report(output) == sample
    prepare.assert_not_called()


@pytest.mark.parametrize("command", ["run", "resume"])
def test_checkpoint_cannot_be_overwritten_by_report_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str,
) -> None:
    path = tmp_path / "checkpoints.sqlite"
    path.write_text("preserve existing data", encoding="utf-8")
    execute = Mock()
    monkeypatch.setattr(cli, "run_verification", execute)
    args = arguments(tmp_path) if command == "run" else ["resume", "1" * 32]
    result = runner.invoke(
        cli.app, [*args, "--checkpoint", str(path), "--json", str(path)]
    )
    assert result.exit_code == 2
    assert path.read_text(encoding="utf-8") == "preserve existing data"
    execute.assert_not_called()


def test_resume_missing_checkpoint_has_a_clear_error_and_creates_no_store(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing.sqlite"
    result = runner.invoke(cli.app, ["resume", "1" * 32, "--checkpoint", str(path),
                                    "--json", str(tmp_path / "out.json")])
    assert result.exit_code == 2
    assert "does not exist" in result.output
    assert not path.exists()
