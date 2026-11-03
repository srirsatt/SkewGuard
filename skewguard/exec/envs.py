"""Records and uv command helpers for per-checkout Python environments."""

# python + postgressql environment creator for base and head branches

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


def run_uv(config: RunConfig, checkout: Path, *arguments: str) -> str:
    """Run uv inside a checkout with bounded execution and captured output."""
    try:
        result = subprocess.run(
            ["uv", "--no-progress", *arguments],
            cwd=checkout,
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
    # create a new python 3.12 environment for checkout
    checkout = checkout.expanduser().resolve() # find the path
    if not checkout.is_dir():
        raise EnvironmentSetupError("checkout must be an existing directory")
    venv = checkout / ".skewguard-venv" # venv for python
    if venv.exists() or venv.is_symlink():
        raise EnvironmentSetupError(f"environment already exists at : {venv}")

    # run the env
    run_uv(config, checkout, "venv", "--no-project", "--no-config", "--no-python-downloads", "--python", "3.12", str(venv))
    python = environment_python(venv)
    if not python.is_file():
        raise EnvironmentSetupError(f"uv didn't create a python executable: {python}")

    return PythonEnvironment(checkout=checkout, venv=venv, python=python)


