"""Check CLI argument validation, status codes, report I/O, and resource ownership."""

from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

import skewguard.cli as cli
from skewguard.config import RunConfig
from skewguard.exec import verify
from skewguard.exec.db import DatabaseConnection
from skewguard.exec.envs import EnvironmentSetupError, PythonEnvironment
from skewguard.exec.probes import ProbeExecutionError
from skewguard.exec.worktree import CheckoutPair
from skewguard.report.json_report import RunReport, read_json_report, write_json_report
from skewguard.report.markdown import render_markdown

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
    assert execute.call_args.kwargs == {
        "settings_module": "shop.settings",
        "seed_fixture": "products",
    }


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


def test_execution_errors_have_a_distinct_exit_code_and_do_not_create_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "run_verification",
        Mock(side_effect=ProbeExecutionError("worker unavailable")),
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


@pytest.mark.parametrize("failure", [None, "install", "execute"])
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
        side_effect=ProbeExecutionError("execution failed")
        if failure == "execute"
        else None,
        return_value=sample.verification,
    )
    monkeypatch.setattr(cli.worktree, "managed_worktrees", Mock(return_value=worktrees))
    monkeypatch.setattr(cli.db, "managed_postgres", start_database)
    monkeypatch.setattr(cli.envs, "create_environment", create)
    monkeypatch.setattr(cli.envs, "install_dependencies", install)
    monkeypatch.setattr(verify, "verify_matrix", execute)

    if failure is not None:
        with pytest.raises((EnvironmentSetupError, ProbeExecutionError)):
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
        assert execute.call_args.args == (config, *environments, server)
        assert execute.call_args.kwargs == {
            "settings_module": "shop.settings",
            "seed_fixture": "products",
        }
    worktrees.__exit__.assert_called_once()
    if failure == "install":
        start_database.assert_not_called()
    else:
        postgres.__exit__.assert_called_once()
