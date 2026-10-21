"""Prepare isolated Git checkouts for a pair of application releases."""

import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from skewguard.config import RunConfig


class GitError(RuntimeError):
    """A Git operation failed or exceeded its timeout."""


class CheckoutPair(BaseModel):
    """Paths and pinned commit IDs for two temporary release checkouts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base: Path
    head: Path
    base_sha: str
    head_sha: str


def run_git(config: RunConfig, *arguments: str) -> str:
    """Run Git in the target repository with a timeout and captured output."""
    command = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "submodule.recurse=false",
        "-C",
        str(config.repo_path),
        *arguments,
    ]
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.timeout_seconds,
        )
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or error.stdout.strip() or "Git command failed"
        raise GitError(f"git {' '.join(arguments)}: {detail}") from error
    except subprocess.TimeoutExpired as error:
        raise GitError(
            f"git {' '.join(arguments)} timed out after {config.timeout_seconds}s"
        ) from error
    except OSError as error:
        raise GitError(f"could not run Git: {error}") from error

    return result.stdout.strip()


def resolve_commit(config: RunConfig, reference: str) -> str:
    """Resolve a revision to a commit, treating it as data rather than an option."""
    return run_git(
        config, "rev-parse", "--verify", "--end-of-options", f"{reference}^{{commit}}"
    )


def add_worktree(config: RunConfig, destination: Path, commit: str) -> None:
    """Create a detached checkout at a run-owned destination."""
    run_git(config, "worktree", "add", "--detach", str(destination), commit)


def remove_worktree(config: RunConfig, destination: Path) -> None:
    """Remove a run-owned checkout, including files generated during execution."""
    run_git(config, "worktree", "remove", "--force", str(destination))


@contextmanager
def managed_worktrees(config: RunConfig) -> Generator[CheckoutPair, None, None]:
    """Provide detached base/head checkouts at pinned commits for one context.

    Resolve both references before creating checkouts. Yield distinct base and
    head paths beneath a temporary directory. Remove successfully created
    worktrees on context exit, including when later setup or the caller fails.
    Preserve the original checkout, its uncommitted files, and unrelated worktrees.
    """
    raise NotImplementedError("Worktree lifecycle management is not implemented")
