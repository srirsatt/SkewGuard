"""Build and validate reports whose conclusions follow the saved execution evidence."""

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from skewguard.config import RunConfig
from skewguard.exec.diff import compare_matrix
from skewguard.exec.types import (
    CandidateDivergence,
    Cell,
    ComparisonIssue,
    ComparisonPolicy,
    VerificationResult,
)
from skewguard.exec.verify import classify_reproductions
from skewguard.exec.worktree import CheckoutPair

CheckStatus = Literal["break_detected", "no_break_detected", "inconclusive"]


class ReportContext(BaseModel):
    """Portable run inputs, without requiring the source repository to still exist."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repo_path: Path
    backend_path: Path
    base_ref: str = Field(min_length=1)
    head_ref: str = Field(min_length=1)
    base_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    head_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    settings_module: str = Field(min_length=1)
    seed_fixture: str | None = None
    timeout_seconds: int = Field(gt=0, strict=True)
    probe_row_limit: int = Field(gt=0, strict=True)
    comparison_policies: dict[str, ComparisonPolicy] = Field(
        default_factory=dict[str, ComparisonPolicy]
    )


class ReportSummary(BaseModel):
    """Observed results of the executed checks; no field claims universal safety."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: CheckStatus
    deploy_window: CheckStatus
    rollback: CheckStatus
    confirmed: int = Field(ge=0, strict=True)
    flaky: int = Field(ge=0, strict=True)
    inconclusive: int = Field(ge=0, strict=True)
    unverified: int = Field(ge=0, strict=True)
    issue_observations: int = Field(ge=0, strict=True)
    failed_replays: int = Field(ge=0, strict=True)


def issue_observations(
    verification: VerificationResult,
) -> list[tuple[str, ComparisonIssue]]:
    """Retain repeated issue observations with the execution that produced them."""
    observations = [
        ("initial", issue) for issue in verification.initial_comparison.issues
    ]
    for attempt in verification.attempts:
        if attempt.comparison is not None:
            observations.extend(
                (f"replay {attempt.number}", issue)
                for issue in attempt.comparison.issues
            )
    return observations


def replay_only_candidates(
    verification: VerificationResult,
) -> list[tuple[int, CandidateDivergence]]:
    """Find unique checks first failing during replays, without confirming them."""
    seen = {
        (item.cell, item.scenario_id)
        for item in verification.initial_comparison.candidates
    }
    additional: list[tuple[int, CandidateDivergence]] = []
    for attempt in verification.attempts:
        if attempt.comparison is not None:
            for candidate in attempt.comparison.candidates:
                key = (candidate.cell, candidate.scenario_id)
                if key not in seen:
                    additional.append((attempt.number, candidate))
                    seen.add(key)
    return additional


def summarize_verification(verification: VerificationResult) -> ReportSummary:
    """Prioritize reproduced breaks, retaining uncertainty when none is confirmed."""
    decisions = verification.decisions
    issues = issue_observations(verification)
    additional = replay_only_candidates(verification)
    failed_replays = sum(attempt.error is not None for attempt in verification.attempts)

    def status_for(cell: Cell | None = None) -> CheckStatus:
        relevant = [
            item for item in decisions if cell is None or item.candidate.cell == cell
        ]
        if any(item.status == "confirmed" for item in relevant):
            return "break_detected"
        if (
            issues
            or failed_replays
            or any(item.status != "confirmed" for item in relevant)
            or any(
                cell is None or candidate.cell == cell for _, candidate in additional
            )
        ):
            return "inconclusive"
        return "no_break_detected"

    return ReportSummary(
        status=status_for(),
        deploy_window=status_for(Cell.A0D1),
        rollback=status_for(Cell.A0D1W),
        confirmed=sum(item.status == "confirmed" for item in decisions),
        flaky=sum(item.status == "flaky" for item in decisions),
        inconclusive=sum(item.status == "inconclusive" for item in decisions),
        unverified=len(additional),
        issue_observations=len(issues),
        failed_replays=failed_replays,
    )


class RunReport(BaseModel):
    """Versioned raw evidence and a checked, deterministic summary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    context: ReportContext
    verification: VerificationResult
    summary: ReportSummary

    @model_validator(mode="after")
    def validate_evidence_and_summary(self) -> Self:
        """Recompute comparisons and decisions rather than trusting saved labels."""
        verification = self.verification
        policies = self.context.comparison_policies
        if compare_matrix(verification.initial_cells, policies=policies) != (
            verification.initial_comparison
        ):
            raise ValueError("initial comparison conflicts with its cell evidence")
        for attempt in verification.attempts:
            if (
                attempt.cells is not None
                and compare_matrix(attempt.cells, policies=policies)
                != attempt.comparison
            ):
                raise ValueError("replay comparison conflicts with its cell evidence")
        expected = (
            classify_reproductions(
                verification.initial_comparison, verification.attempts
            )
            if verification.initial_comparison.candidates
            else []
        )
        if verification.decisions != expected:
            raise ValueError("reproduction decisions conflict with replay evidence")
        if self.summary != summarize_verification(verification):
            raise ValueError("report summary conflicts with verification evidence")
        return self


def build_report(
    config: RunConfig,
    checkouts: CheckoutPair,
    verification: VerificationResult,
    *,
    settings_module: str,
    seed_fixture: str | None = None,
    policies: dict[str, ComparisonPolicy] | None = None,
) -> RunReport:
    """Capture pinned commits and verification evidence before checkouts disappear."""
    context = ReportContext(
        repo_path=config.repo_path,
        backend_path=config.backend_path,
        base_ref=config.base_ref,
        head_ref=config.head_ref,
        base_sha=checkouts.base_sha,
        head_sha=checkouts.head_sha,
        settings_module=settings_module,
        seed_fixture=seed_fixture,
        timeout_seconds=config.timeout_seconds,
        probe_row_limit=config.probe_row_limit,
        comparison_policies=dict(policies or {}),
    )
    return RunReport(
        context=context,
        verification=verification,
        summary=summarize_verification(verification),
    )


def write_text(path: Path, content: str) -> Path:
    """Replace an output file atomically, removing temporary files on failure."""
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=".skewguard-",
            delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(content)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def write_json_report(report: RunReport, path: Path) -> Path:
    """Save complete, schema-validated evidence as standard JSON."""
    content = json.dumps(
        report.model_dump(mode="json"), indent=2, ensure_ascii=False, allow_nan=False
    )
    return write_text(path, content + "\n")


def read_json_report(path: Path) -> RunReport:
    """Load a report without Django, Docker, or the original repository."""
    return RunReport.model_validate_json(path.expanduser().read_text(encoding="utf-8"))
