"""Validated settings shared by the interfaces and execution layer."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RunConfig(BaseModel):
    """Describe one run before preparing worktrees or starting a database.

    The repository must be an existing local directory. Git repository and
    reference checks belong to the future worktree preparation step.
    Numeric limits require actual integers; interfaces parse textual arguments
    before constructing this model.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )

    repo_path: Path
    base_ref: str = Field(min_length=1)
    head_ref: str = Field(min_length=1)
    timeout_seconds: int = Field(default=60, gt=0, strict=True)
    probe_row_limit: int = Field(default=10, gt=0, strict=True)

    @field_validator("repo_path")
    @classmethod
    def resolve_repo_directory(cls, value: Path) -> Path:
        """Keep the repository path stable when subprocesses change directory."""
        directory = value.expanduser().resolve()
        if not directory.is_dir():
            raise ValueError("repo_path must point to an existing directory")
        return directory
