"""Records and uv command helpers for per-checkout Python environments."""

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
