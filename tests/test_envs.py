"""Check uv command failures and independent Python environment behavior."""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Never

import pytest

from skewguard.config import RunConfig
from skewguard.exec.envs import (
    EnvironmentSetupError,
    PythonEnvironment,
    environment_python,
    run_uv,
)


@pytest.fixture
def config(tmp_path: Path) -> RunConfig:
    return RunConfig(repo_path=tmp_path, base_ref="main", head_ref="HEAD")


@pytest.fixture
def offline_uv(monkeypatch: pytest.MonkeyPatch) -> None:
    if shutil.which("uv") is None:
        pytest.skip("uv must be on PATH to exercise environment creation")
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.setenv("UV_NO_CACHE", "1")
    monkeypatch.delenv("UV_VENV_SEED", raising=False)


def test_uv_output_is_captured(config: RunConfig, offline_uv: None) -> None:
    assert run_uv(config, config.repo_path, "--version").startswith("uv ")


def test_uv_failure_preserves_the_command_error(
    config: RunConfig, offline_uv: None
) -> None:
    with pytest.raises(
        EnvironmentSetupError, match="unknown-skewguard-command"
    ) as error:
        run_uv(config, config.repo_path, "unknown-skewguard-command")

    assert isinstance(error.value.__cause__, subprocess.CalledProcessError)


def test_uv_timeout_is_reported(
    config: RunConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def time_out(*args: object, **kwargs: object) -> Never:
        assert kwargs["timeout"] == config.timeout_seconds
        raise subprocess.TimeoutExpired("uv", config.timeout_seconds)

    monkeypatch.setattr(subprocess, "run", time_out)

    with pytest.raises(EnvironmentSetupError, match="timed out"):
        run_uv(config, config.repo_path, "--version")


def test_missing_uv_is_reported(
    config: RunConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing_executable(*args: object, **kwargs: object) -> Never:
        raise FileNotFoundError("uv executable not found")

    monkeypatch.setattr(subprocess, "run", missing_executable)

    with pytest.raises(EnvironmentSetupError, match="could not run uv"):
        run_uv(config, config.repo_path, "--version")


def run_python(environment: PythonEnvironment, source: str) -> str:
    return subprocess.run(
        [str(environment.python), "-I", "-c", source],
        cwd=environment.checkout,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()


def test_uv_creates_environments_with_separate_package_directories(
    config: RunConfig, offline_uv: None
) -> None:
    environments: list[PythonEnvironment] = []

    for release in ("base", "head"):
        checkout = config.repo_path / release
        checkout.mkdir()
        venv = checkout / ".skewguard-venv"
        run_uv(
            config,
            checkout,
            "venv",
            "--no-project",
            "--no-config",
            "--no-python-downloads",
            "--python",
            sys.executable,
            str(venv),
        )
        environment = PythonEnvironment(
            checkout=checkout, venv=venv, python=environment_python(venv)
        )
        assert environment.python.is_file()
        assert (
            Path(run_python(environment, "import os; print(os.getcwd())")) == checkout
        )
        prefixes = json.loads(
            run_python(
                environment,
                "import json, sys; print(json.dumps([sys.prefix, sys.base_prefix]))",
            )
        )
        assert Path(prefixes[0]) == venv
        assert prefixes[0] != prefixes[1]
        environments.append(environment)

    base, head = environments
    assert base.python != head.python
    site_packages = Path(
        run_python(base, "import sysconfig; print(sysconfig.get_path('purelib'))")
    )
    (site_packages / "skewguard_isolation_marker.py").write_text("VALUE = 'base'\n")
    find_marker = (
        "import importlib.util; "
        "print(importlib.util.find_spec('skewguard_isolation_marker') is not None)"
    )
    assert run_python(base, find_marker) == "True"
    assert run_python(head, find_marker) == "False"
