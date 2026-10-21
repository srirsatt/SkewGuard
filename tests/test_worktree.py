"""Verify revision resolution and the lifecycle of isolated Git checkouts."""

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Never

import pytest

from skewguard.config import RunConfig
from skewguard.exec import worktree


@dataclass(frozen=True)
class Repository:
    path: Path
    base_sha: str
    head_sha: str


def git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=SkewGuard Tests",
            "-c",
            "user.email=tests@example.invalid",
            "-C",
            str(repo),
            *arguments,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    repo = tmp_path / "repository"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    (repo / "release.txt").write_text("old release\n")
    git(repo, "add", "release.txt")
    git(repo, "commit", "-m", "Add old release")
    base_sha = git(repo, "rev-parse", "HEAD")
    (repo / "release.txt").write_text("new release\n")
    git(repo, "commit", "-am", "Add new release")
    return Repository(repo, base_sha, git(repo, "rev-parse", "HEAD"))


@pytest.fixture
def config(repository: Repository) -> RunConfig:
    return RunConfig(
        repo_path=repository.path,
        base_ref=repository.base_sha,
        head_ref=repository.head_sha,
    )


def test_resolve_commit_accepts_a_revision_expression(
    config: RunConfig, repository: Repository
) -> None:
    assert worktree.resolve_commit(config, "HEAD~1") == repository.base_sha


@pytest.mark.parametrize("reference", ["missing-release", "--help", "HEAD:release.txt"])
def test_resolve_commit_rejects_invalid_commit_references(
    config: RunConfig, reference: str
) -> None:
    with pytest.raises(worktree.GitError):
        worktree.resolve_commit(config, reference)


def test_resolve_commit_rejects_a_directory_without_git(tmp_path: Path) -> None:
    config = RunConfig(repo_path=tmp_path, base_ref="main", head_ref="HEAD")

    with pytest.raises(worktree.GitError):
        worktree.resolve_commit(config, config.base_ref)


def test_git_timeout_is_reported_as_a_git_error(
    config: RunConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def time_out(*args: object, **kwargs: object) -> Never:
        raise subprocess.TimeoutExpired("git", config.timeout_seconds)

    monkeypatch.setattr(subprocess, "run", time_out)

    with pytest.raises(worktree.GitError, match="timed out"):
        worktree.resolve_commit(config, config.base_ref)


def test_worktrees_use_committed_files_and_preserve_original_changes(
    config: RunConfig, repository: Repository
) -> None:
    (repository.path / "release.txt").write_text("uncommitted change\n")
    (repository.path / "local.txt").write_text("untracked file\n")
    original_status = git(repository.path, "status", "--porcelain")
    original_worktrees = git(repository.path, "worktree", "list", "--porcelain")

    with worktree.managed_worktrees(config) as checkouts:
        assert checkouts.base != checkouts.head
        assert checkouts.base.parent == checkouts.head.parent
        assert checkouts.base_sha == repository.base_sha
        assert checkouts.head_sha == repository.head_sha
        assert (checkouts.base / "release.txt").read_text() == "old release\n"
        assert (checkouts.head / "release.txt").read_text() == "new release\n"
        assert git(checkouts.base, "rev-parse", "HEAD") == checkouts.base_sha
        assert git(checkouts.head, "rev-parse", "HEAD") == checkouts.head_sha
        assert git(checkouts.base, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
        assert git(checkouts.head, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"

    assert not checkouts.base.exists()
    assert not checkouts.head.exists()
    assert not checkouts.base.parent.exists()
    assert git(repository.path, "status", "--porcelain") == original_status
    assert git(repository.path, "worktree", "list", "--porcelain") == original_worktrees
    assert git(repository.path, "branch", "--show-current") == "main"
    assert (repository.path / "release.txt").read_text() == "uncommitted change\n"
    assert (repository.path / "local.txt").read_text() == "untracked file\n"


def test_worktrees_are_removed_when_the_caller_fails(
    config: RunConfig, repository: Repository
) -> None:
    original_worktrees = git(repository.path, "worktree", "list", "--porcelain")
    paths: list[Path] = []

    with pytest.raises(RuntimeError, match="probe failed"):
        with worktree.managed_worktrees(config) as checkouts:
            paths.extend([checkouts.base, checkouts.head])
            (checkouts.base / "generated.txt").write_text("temporary output")
            (checkouts.head / "release.txt").write_text("modified by a probe")
            raise RuntimeError("probe failed")

    assert len(paths) == 2
    assert all(not path.exists() for path in paths)
    assert git(repository.path, "worktree", "list", "--porcelain") == original_worktrees


def test_invalid_head_reference_leaves_no_registered_worktrees(
    repository: Repository,
) -> None:
    config = RunConfig(
        repo_path=repository.path,
        base_ref=repository.base_sha,
        head_ref="missing-release",
    )
    original_worktrees = git(repository.path, "worktree", "list", "--porcelain")

    with pytest.raises(worktree.GitError):
        with worktree.managed_worktrees(config):
            pytest.fail("an invalid revision must prevent entering the context")

    assert git(repository.path, "worktree", "list", "--porcelain") == original_worktrees


def test_base_worktree_is_removed_if_head_setup_fails(
    config: RunConfig, repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_worktrees = git(repository.path, "worktree", "list", "--porcelain")
    original_add = worktree.add_worktree
    created: list[Path] = []

    def fail_second_add(config: RunConfig, destination: Path, commit: str) -> None:
        if created:
            raise worktree.GitError("head checkout failed")
        original_add(config, destination, commit)
        created.append(destination)

    monkeypatch.setattr(worktree, "add_worktree", fail_second_add)

    with pytest.raises(worktree.GitError, match="head checkout failed"):
        with worktree.managed_worktrees(config):
            pytest.fail("failed setup must prevent entering the context")

    assert len(created) == 1
    assert not created[0].exists()
    assert git(repository.path, "worktree", "list", "--porcelain") == original_worktrees


def test_both_references_may_point_to_the_same_commit(repository: Repository) -> None:
    config = RunConfig(repo_path=repository.path, base_ref="HEAD", head_ref="HEAD")

    with worktree.managed_worktrees(config) as checkouts:
        assert checkouts.base != checkouts.head
        assert checkouts.base_sha == checkouts.head_sha == repository.head_sha


def test_unrelated_worktree_is_preserved(
    config: RunConfig, repository: Repository, tmp_path: Path
) -> None:
    unrelated = tmp_path / "existing-worktree"
    git(repository.path, "worktree", "add", "--detach", str(unrelated), "HEAD")
    original_worktrees = git(repository.path, "worktree", "list", "--porcelain")

    with worktree.managed_worktrees(config):
        assert unrelated.is_dir()

    assert unrelated.is_dir()
    assert git(repository.path, "worktree", "list", "--porcelain") == original_worktrees
