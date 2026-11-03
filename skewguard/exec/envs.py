"""Create isolated Python environments for application checkouts."""

# Python environments for the base and head checkouts.

import os
import subprocess
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from skewguard.config import RunConfig


class EnvironmentSetupError(RuntimeError):
    """A Python environment could not be prepared."""


class PythonEnvironment(BaseModel):
    """Locate a checkout, its virtual environment, and its Python executable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    checkout: Path
    venv: Path
    python: Path


def environment_python(venv: Path) -> Path:
    """Locate the interpreter using the platform's virtual environment layout."""
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def backend_directory(config: RunConfig, checkout: Path) -> Path:
    """Find the backend inside a checkout, rejecting missing or escaping paths."""
    checkout = checkout.resolve()
    backend = (checkout / config.backend_path).resolve()
    if not backend.is_relative_to(checkout):
        raise EnvironmentSetupError("backend_path must stay inside the checkout")
    if not backend.is_dir():
        raise EnvironmentSetupError(f"backend directory does not exist: {backend}")
    return backend


def validate_environment(environment: PythonEnvironment) -> None:
    """Require the virtual environment layout owned by create_environment."""
    expected = environment.checkout.resolve() / ".skewguard-venv"
    if environment.venv != expected or environment.venv.is_symlink():
        raise EnvironmentSetupError("virtual environment must belong to its checkout")
    if not (environment.venv / "pyvenv.cfg").is_file():
        raise EnvironmentSetupError("create the virtual environment before installing")
    if (
        environment.python != environment_python(environment.venv)
        or not environment.python.is_file()
    ):
        raise EnvironmentSetupError("Python executable must belong to the environment")


def run_uv(
    config: RunConfig,
    checkout: Path,
    *arguments: str,
    env_overrides: dict[str, str] | None = None,
) -> str:
    """Run uv inside a checkout with bounded execution and captured output."""
    process_env = os.environ.copy()
    for name in (
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "PYTHONHOME",
        "PYTHONPATH",
        "UV_PROJECT_ENVIRONMENT",
        "UV_PROJECT",
        "UV_WORKING_DIR",
        "UV_PYTHON",
        "UV_SYSTEM_PYTHON",
        "UV_FROZEN",
        "UV_LOCKED",
    ):
        process_env.pop(name, None)
    process_env.update(env_overrides or {})
    try:
        result = subprocess.run(
            ["uv", "--no-progress", *arguments],
            cwd=checkout,
            env=process_env,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.timeout_seconds,
        )
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or error.stdout.strip() or "uv command failed"
        raise EnvironmentSetupError(f"uv {' '.join(arguments)}: {detail}") from error
    except subprocess.TimeoutExpired as error:
        raise EnvironmentSetupError(
            f"uv {' '.join(arguments)} timed out after {config.timeout_seconds}s"
        ) from error
    except OSError as error:
        raise EnvironmentSetupError(f"could not run uv: {error}") from error

    return result.stdout.strip()


# above is helpers to setup and run


def create_environment(config: RunConfig, checkout: Path) -> PythonEnvironment:
    """Create an empty Python 3.12 environment inside a run-owned checkout."""
    checkout = checkout.expanduser().resolve()  # find the path
    if not checkout.is_dir():
        raise EnvironmentSetupError("checkout must be an existing directory")
    venv = checkout / ".skewguard-venv"  # venv for python
    if venv.exists() or venv.is_symlink():
        raise EnvironmentSetupError(f"environment already exists at : {venv}")

    # Create the virtual environment.
    run_uv(
        config,
        checkout,
        "venv",
        "--no-project",
        "--no-config",
        "--no-python-downloads",
        "--python",
        "3.12",
        str(venv),
    )
    python = environment_python(venv)
    if not python.is_file():
        raise EnvironmentSetupError(f"uv didn't create a python executable: {python}")

    return PythonEnvironment(checkout=checkout, venv=venv, python=python)

def install_dependencies(config: RunConfig, environment: PythonEnvironment) -> None:
    # install all backend dependencies once found

    validate_environment(environment)
    backend = backend_directory(config, environment.checkout) # find backend

    lockfile = backend / "uv.lock"
    pyproject = backend / "pyproject.toml"
    requirements = backend / "requirements.txt"

    if lockfile.is_file():
        if not pyproject.is_file():
            raise EnvironmentSetupError("uv.lock requires a pyproject.toml beside it")
    
        run_uv(config, backend, "sync", "--locked", "--no-default-groups", "--project", str(backend), "--python", str(environment.python), "--no-python-downloads", env_overrides={"UV_PROJECT_ENVIRONMENT": str(environment.venv)})
    elif requirements.is_file():
        run_uv(
            config,
            backend,
            "pip",
            "install",
            "--python",
            str(environment.python),
            "--no-python-downloads",
            "--requirements",
            str(requirements),
        )
    else:
        raise EnvironmentSetupError(f"expected uv.lock with pyproject.toml or requirements.txt in {backend}")

    run_uv(config, backend, "pip", "check", "--python", str(environment.python))
