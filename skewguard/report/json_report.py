"""Build and validate reports whose conclusions follow the saved execution evidence."""

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from skewguard.agents.scenario import ScenarioAttempt
from skewguard.agents.seed import SeedAttempt, SeedProposal
from skewguard.config import RunConfig
from skewguard.exec.diff import compare_matrix
from skewguard.exec.scenarios import Scenario
from skewguard.exec.types import (
    CandidateDivergence,
    Cell,
    CellResult,
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
    seed_model: str | None = None
    generated_seed: SeedProposal | None = None
    seed_attempts: tuple[SeedAttempt, ...] = ()
    max_llm_calls: int = Field(default=2, ge=1)
    scenario_model: str | None = None
    scenario_attempts: tuple[ScenarioAttempt, ...] = ()
    scenarios: tuple[Scenario, ...] = ()
    comparison_policies: dict[str, ComparisonPolicy] = Field(
        default_factory=dict[str, ComparisonPolicy]
    )

    @property
    def seed_skip_reason(self) -> str | None:
        if not self.seed_model or self.seed_attempts:
            return None
        if len(self.scenario_attempts) >= self.max_llm_calls:
            return "shared model-call budget exhausted"
        return "no GET scenario available"

    @model_validator(mode="after")
    def validate_generation_history(self) -> Self:
        if len(self.seed_attempts) + len(self.scenario_attempts) > self.max_llm_calls:
            raise ValueError("proposal attempts exceed the shared call budget")
        if self.seed_attempts:
            *previous, final = self.seed_attempts
            if not self.seed_model or len(previous) > 3:
                raise ValueError("seed generation needs a model and allows four turns")
            if any(
                turn.status != "rejected" or turn.scenario_id != final.scenario_id
                for turn in previous
            ):
                raise ValueError("only rejected seeds for the same GET can be retried")
            if final.status == "accepted" and final.proposal is None:
                raise ValueError("accepted seed is missing its row")
            expected = final.proposal if final.status == "accepted" else None
            if self.generated_seed != expected:
                raise ValueError("seed history conflicts with the matrix seed")
        if not self.scenario_attempts:
            return self
        *earlier, last = self.scenario_attempts
        if not self.scenario_model or len(earlier) > 1:
            raise ValueError("generation requires a model and at most two turns")
        if any(turn.status != "rejected" for turn in earlier):
            raise ValueError("only a rejected proposal may be retried")
        if last.status == "accepted" and last.scenario not in self.scenarios:
            raise ValueError("accepted generated scenario is missing from the plan")
        return self

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
        accepted = validate_http_plan(
            verification.initial_cells, self.context.scenarios
        )
        if compare_matrix(verification.initial_cells, policies=policies) != (
            verification.initial_comparison
        ):
            raise ValueError("initial comparison conflicts with its cell evidence")
        for attempt in verification.attempts:
            if attempt.cells is not None:
                validate_http_plan(attempt.cells, accepted)
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


def validate_http_plan(
    cells: list[CellResult], proposals: tuple[Scenario, ...]
) -> tuple[Scenario, ...]:
    """Account for every supplied proposal and preserve the requests used in replays."""
    baseline = next((cell for cell in cells if cell.cell == Cell.A0D0), None)
    if baseline is None:
        raise ValueError("HTTP qualification requires baseline cell evidence")
    rejected = {issue.scenario_id for issue in baseline.execution_issues}
    proposed_ids = {scenario.scenario_id for scenario in proposals}
    accepted = tuple(s for s in proposals if s.scenario_id not in rejected)
    if (
        len(proposed_ids) != len(proposals)
        or not proposed_ids.issuperset(rejected)
        or accepted != baseline.qualified_scenarios
    ):
        raise ValueError("qualified HTTP checks conflict with proposed scenarios")
    return accepted


def build_report(
    config: RunConfig,
    checkouts: CheckoutPair,
    verification: VerificationResult,
    *,
    settings_module: str,
    seed_fixture: str | None = None,
    policies: dict[str, ComparisonPolicy] | None = None,
    scenario_attempts: tuple[ScenarioAttempt, ...] = (),
    seed_attempts: tuple[SeedAttempt, ...] = (),
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
        scenarios=config.scenarios,
        scenario_model=config.scenario_model,
        seed_model=config.seed_model,
        generated_seed=config.generated_seed,
        seed_attempts=seed_attempts,
        max_llm_calls=config.max_llm_calls,
        scenario_attempts=scenario_attempts,
        comparison_policies={
            **{s.scenario_id: s.comparison_policy() for s in config.scenarios},
            **(policies or {}),
        },
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
