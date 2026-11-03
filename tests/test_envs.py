"""Check environment creation, existing-path protection, and package isolation."""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Never

import pytest

from skewguard.config import RunConfig
from skewguard.exec import envs
from skewguard.exec.envs import (
    EnvironmentSetupError,
    PythonEnvironment,
    create_environment,
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


def test_create_environment_produces_isolated_python_312_environments(
    config: RunConfig, offline_uv: None
) -> None:
    environments: list[PythonEnvironment] = []

    for release in ("base", "head"):
        checkout = config.repo_path / release
        checkout.mkdir()
        venv = checkout / ".skewguard-venv"
        environment = create_environment(config, checkout)
        assert environment.checkout == checkout
        assert environment.venv == venv
        assert environment.python.is_file()
        assert (
            Path(run_python(environment, "import os; print(os.getcwd())")) == checkout
        )
        prefixes = json.loads(
            run_python(
                environment,
                "import json, sys; "
                "print(json.dumps([sys.prefix, sys.base_prefix, "
                "list(sys.version_info[:2])]))",
            )
        )
        assert Path(prefixes[0]) == venv
        assert prefixes[0] != prefixes[1]
        assert prefixes[2] == [3, 12]
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


def test_create_environment_resolves_relative_checkout_paths(
    config: RunConfig, offline_uv: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = config.repo_path / "old release"
    checkout.mkdir()
    monkeypatch.chdir(config.repo_path)

    environment = create_environment(config, Path("old release"))

    assert environment.checkout == checkout
    assert environment.venv == checkout / ".skewguard-venv"
    assert environment.python.is_absolute()
    assert environment.python.is_file()


@pytest.mark.parametrize("path_kind", ["missing", "file"])
def test_create_environment_rejects_invalid_checkouts(
    config: RunConfig, path_kind: str
) -> None:
    checkout = config.repo_path / "checkout"
    if path_kind == "file":
        checkout.write_text("application source")

    with pytest.raises(EnvironmentSetupError, match="existing directory"):
        create_environment(config, checkout)

    if path_kind == "file":
        assert checkout.read_text() == "application source"
    else:
        assert not checkout.exists()


@pytest.mark.parametrize("path_kind", ["directory", "file", "dangling_symlink"])
def test_create_environment_preserves_existing_targets(
    config: RunConfig, path_kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    venv = config.repo_path / ".skewguard-venv"
    if path_kind == "directory":
        venv.mkdir()
        (venv / "keep.txt").write_text("existing data")
    elif path_kind == "file":
        venv.write_text("existing data")
    else:
        venv.symlink_to(config.repo_path / "missing-target", target_is_directory=True)

    def unexpected_uv(*args: object, **kwargs: object) -> Never:
        raise AssertionError("an existing target must be rejected before running uv")

    monkeypatch.setattr(envs, "run_uv", unexpected_uv)

    with pytest.raises(EnvironmentSetupError, match="already exists"):
        create_environment(config, config.repo_path)

    if path_kind == "directory":
        assert (venv / "keep.txt").read_text() == "existing data"
    elif path_kind == "file":
        assert venv.read_text() == "existing data"
    else:
        assert venv.is_symlink()
        assert venv.readlink() == config.repo_path / "missing-target"
        assert not (config.repo_path / "missing-target").exists()


def test_create_environment_requires_a_python_executable(
    config: RunConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def create_nothing(*args: object, **kwargs: object) -> str:
        return ""

    monkeypatch.setattr(envs, "run_uv", create_nothing)

    with pytest.raises(EnvironmentSetupError, match="executable"):
        create_environment(config, config.repo_path)
