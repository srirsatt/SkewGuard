"""Check configuration boundaries before any execution resources are created."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from skewguard.config import RunConfig


def test_relative_repository_stays_stable_after_changing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "shop"
    repository.mkdir()
    monkeypatch.chdir(tmp_path)

    config = RunConfig.model_validate(
        {"repo_path": "shop", "base_ref": " main ", "head_ref": "HEAD"}
    )
    monkeypatch.chdir(repository)

    assert config.repo_path == repository.resolve()
    assert config.base_ref == "main"
    assert config.head_ref == "HEAD"
    assert config.timeout_seconds == 60
    assert config.probe_row_limit == 10
    assert RunConfig.model_validate_json(config.model_dump_json()) == config


@pytest.mark.parametrize("path_kind", ["missing", "file"])
def test_repository_must_be_an_existing_directory(
    tmp_path: Path, path_kind: str
) -> None:
    candidate = tmp_path / "shop"
    if path_kind == "file":
        candidate.write_text("not a directory")

    with pytest.raises(ValidationError, match="existing directory"):
        RunConfig(repo_path=candidate, base_ref="main", head_ref="HEAD")


@pytest.mark.parametrize("field", ["base_ref", "head_ref"])
@pytest.mark.parametrize("bad_reference", ["", "   "])
def test_empty_references_are_rejected(
    tmp_path: Path, field: str, bad_reference: str
) -> None:
    values: dict[str, object] = {
        "repo_path": tmp_path,
        "base_ref": "main",
        "head_ref": "HEAD",
    }
    values[field] = bad_reference

    with pytest.raises(ValidationError) as error:
        RunConfig.model_validate(values)

    assert error.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize("field", ["timeout_seconds", "probe_row_limit"])
@pytest.mark.parametrize("bad_limit", [0, -1, True, 1.5, "30"])
def test_limits_require_positive_integers(
    tmp_path: Path, field: str, bad_limit: object
) -> None:
    values: dict[str, object] = {
        "repo_path": tmp_path,
        "base_ref": "main",
        "head_ref": "HEAD",
    }
    values[field] = bad_limit

    with pytest.raises(ValidationError) as error:
        RunConfig.model_validate(values)

    assert error.value.errors()[0]["loc"] == (field,)


def test_misspelled_setting_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="timeout_second"):
        RunConfig.model_validate(
            {
                "repo_path": tmp_path,
                "base_ref": "main",
                "head_ref": "HEAD",
                "timeout_second": 120,
            }
        )


def test_validated_configuration_cannot_be_changed_mid_run(tmp_path: Path) -> None:
    config = RunConfig(repo_path=tmp_path, base_ref="main", head_ref="HEAD")

    with pytest.raises(ValidationError, match="frozen"):
        config.timeout_seconds = -1
