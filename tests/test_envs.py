"""Check environment creation, existing-path protection, and package isolation."""

import base64
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Never

import pytest

from skewguard.config import RunConfig
from skewguard.exec import envs
from skewguard.exec.envs import (
    EnvironmentSetupError,
    PythonEnvironment,
    backend_directory,
    create_environment,
    environment_python,
    run_uv,
    validate_environment,
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


@pytest.mark.parametrize("relative", [".", "services/backend"])
def test_backend_directory_uses_the_selected_checkout(
    config: RunConfig, relative: str
) -> None:
    selected = config.model_copy(update={"backend_path": Path(relative)})
    checkout = config.repo_path / "release"
    backend = checkout / relative
    backend.mkdir(parents=True)

    assert backend_directory(selected, checkout) == backend.resolve()


def test_backend_directory_rejects_missing_directories(config: RunConfig) -> None:
    selected = config.model_copy(update={"backend_path": Path("backend")})

    with pytest.raises(EnvironmentSetupError, match="does not exist"):
        backend_directory(selected, config.repo_path)


def test_backend_directory_rejects_symlinks_outside_checkout(config: RunConfig) -> None:
    selected = config.model_copy(update={"backend_path": Path("backend")})
    checkout = config.repo_path / "release"
    checkout.mkdir()
    outside = config.repo_path / "outside"
    outside.mkdir()
    (checkout / "backend").symlink_to(outside, target_is_directory=True)

    with pytest.raises(EnvironmentSetupError, match="inside the checkout"):
        backend_directory(selected, checkout)


def test_validate_environment_requires_an_existing_venv(config: RunConfig) -> None:
    venv = config.repo_path / ".skewguard-venv"
    environment = PythonEnvironment(
        checkout=config.repo_path, venv=venv, python=environment_python(venv)
    )

    with pytest.raises(EnvironmentSetupError, match="before installing"):
        validate_environment(environment)


@pytest.mark.parametrize("field", ["venv", "python"])
def test_validate_environment_rejects_redirected_install_targets(
    config: RunConfig, offline_uv: None, field: str
) -> None:
    environment = create_environment(config, config.repo_path)
    redirected = environment.model_copy(update={field: config.repo_path / "other"})

    with pytest.raises(EnvironmentSetupError, match="belong"):
        validate_environment(redirected)


def make_dependency_wheel(directory: Path, version: str) -> Path:
    """Build a tiny local wheel so installation tests never need a registry."""
    directory.mkdir(parents=True, exist_ok=True)
    name = "skewguard_dependency_fixture"
    dist_info = f"{name}-{version}.dist-info"
    files = {
        f"{name}/__init__.py": f"VERSION = {version!r}\n".encode(),
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.1\n"
            "Name: skewguard-dependency-fixture\n"
            f"Version: {version}\n"
            "Requires-Python: >=3.12\n\n"
        ).encode(),
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    records = io.StringIO()
    writer = csv.writer(records)
    for path, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest())
        writer.writerow([path, f"sha256={digest.decode().rstrip('=')}", len(data)])
    writer.writerow([f"{dist_info}/RECORD", "", ""])
    files[f"{dist_info}/RECORD"] = records.getvalue().encode()
    wheel = directory / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, data in files.items():
            archive.writestr(path, data)
    return wheel


def dependency_version(environment: PythonEnvironment) -> str:
    return run_python(
        environment,
        "from importlib.metadata import version; "
        "print(version('skewguard-dependency-fixture'))",
    )


def test_requirements_installs_use_each_backend_and_explicit_python(
    config: RunConfig, offline_uv: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = config.model_copy(update={"backend_path": Path("backend")})
    protected = config.repo_path / "active-environment"
    protected.mkdir()
    protected_env = create_environment(config, protected)
    original_config = (protected_env.venv / "pyvenv.cfg").read_bytes()
    monkeypatch.setenv("VIRTUAL_ENV", str(protected_env.venv))
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(protected_env.venv))
    monkeypatch.setenv("UV_WORKING_DIR", str(protected))
    monkeypatch.setenv("UV_PYTHON", str(protected_env.python))
    monkeypatch.setenv("UV_SYSTEM_PYTHON", "true")
    environments: list[PythonEnvironment] = []

    for release, version in (("base", "1.0.0"), ("head", "2.0.0")):
        checkout = config.repo_path / release
        backend = checkout / "backend"
        backend.mkdir(parents=True)
        wheel = make_dependency_wheel(backend / "wheels", version)
        requirements = backend / "requirements.txt"
        requirements.write_text(f"./wheels/{wheel.name}\n")
        original_requirements = requirements.read_bytes()
        environment = create_environment(selected, checkout)
        validate_environment(environment)
        run_uv(
            selected,
            backend_directory(selected, checkout),
            "pip",
            "install",
            "--python",
            str(environment.python),
            "--no-python-downloads",
            "--requirements",
            str(requirements),
        )
        run_uv(selected, backend, "pip", "check", "--python", str(environment.python))
        assert dependency_version(environment) == version
        assert requirements.read_bytes() == original_requirements
        assert not (backend / ".venv").exists()
        environments.append(environment)

    assert dependency_version(environments[0]) == "1.0.0"
    assert dependency_version(environments[1]) == "2.0.0"
    protected_has_no_dependency = run_python(
        protected_env,
        "import importlib.util; "
        "print(importlib.util.find_spec('skewguard_dependency_fixture') is None)",
    )
    assert protected_has_no_dependency == "True"
    assert (protected_env.venv / "pyvenv.cfg").read_bytes() == original_config
    assert os.environ["VIRTUAL_ENV"] == str(protected_env.venv)
    assert os.environ["UV_SYSTEM_PYTHON"] == "true"


def test_locked_sync_targets_our_venv_and_refuses_stale_metadata(
    config: RunConfig, offline_uv: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = config.model_copy(update={"backend_path": Path("backend")})
    checkout = config.repo_path / "head"
    backend = checkout / "backend"
    backend.mkdir(parents=True)
    wheel = make_dependency_wheel(backend / "wheels", "2.0.0")
    pyproject = backend / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "fixture-backend"\nversion = "0.1.0"\n'
        'requires-python = ">=3.12,<3.13"\n'
        'dependencies = ["skewguard-dependency-fixture==2.0.0"]\n'
        "[tool.uv]\npackage = false\n"
        "[tool.uv.sources]\n"
        f'skewguard-dependency-fixture = {{path = "wheels/{wheel.name}"}}\n'
    )
    environment = create_environment(selected, checkout)
    validate_environment(environment)
    run_uv(
        selected,
        backend,
        "lock",
        "--project",
        str(backend),
        "--python",
        str(environment.python),
        "--no-python-downloads",
    )
    lockfile = backend / "uv.lock"
    original_lock = lockfile.read_bytes()
    original_project = pyproject.read_text()
    protected = config.repo_path / "protected"
    protected.mkdir()
    protected_env = create_environment(config, protected)
    protected_wheel = make_dependency_wheel(protected / "wheels", "1.0.0")
    run_uv(
        config,
        protected,
        "pip",
        "install",
        "--python",
        str(protected_env.python),
        str(protected_wheel),
    )
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(protected_env.venv))
    monkeypatch.setenv("VIRTUAL_ENV", str(protected_env.venv))
    monkeypatch.setenv("UV_WORKING_DIR", str(protected))
    monkeypatch.setenv("UV_FROZEN", "true")
    sync_arguments = (
        "sync",
        "--locked",
        "--no-default-groups",
        "--project",
        str(backend),
        "--python",
        str(environment.python),
        "--no-python-downloads",
    )
    target = {"UV_PROJECT_ENVIRONMENT": str(environment.venv)}

    run_uv(selected, backend, *sync_arguments, env_overrides=target)

    assert dependency_version(environment) == "2.0.0"
    assert dependency_version(protected_env) == "1.0.0"
    assert lockfile.read_bytes() == original_lock
    assert pyproject.read_text() == original_project
    assert not (backend / ".venv").exists()
    assert os.environ["UV_PROJECT_ENVIRONMENT"] == str(protected_env.venv)
    assert os.environ["UV_FROZEN"] == "true"

    make_dependency_wheel(backend / "wheels", "3.0.0")
    pyproject.write_text(original_project.replace("2.0.0", "3.0.0"))
    with pytest.raises(EnvironmentSetupError):
        run_uv(selected, backend, *sync_arguments, env_overrides=target)

    assert lockfile.read_bytes() == original_lock
    assert dependency_version(environment) == "2.0.0"
    assert dependency_version(protected_env) == "1.0.0"
