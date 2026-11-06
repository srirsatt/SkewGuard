"""Shared records for identifying matrix cells and recording observed outcomes."""

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class Cell(StrEnum):
    A0D0 = "A0D0"  # a - application, d - db. old app, old database
    A0D1 = "A0D1"  # old app, new db
    A1D1 = "A1D1"  # new app new db
    A0D1W = "A0D1w"  # old code against migrated db after a new code write


class Outcome(BaseModel):
    """The observed result of executing one probe or scenario."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str = Field(min_length=1)
    status: Literal["ok", "exception", "http_status"]
    exception_type: str | None = Field(default=None, min_length=1)
    exception_message: str | None = None
    http_status: int | None = Field(default=None, ge=100, le=599, strict=True)
    body: JsonValue = None
    integrity_error: bool = False

    @model_validator(mode="after")
    def validate_status_fields(self) -> Self:
        """Reject evidence that conflicts with the recorded status."""
        if self.status == "exception":
            if self.exception_type is None or self.exception_message is None:
                raise ValueError(
                    "exception outcomes require exception_type and exception_message"
                )
        elif self.exception_type is not None or self.exception_message is not None:
            raise ValueError("exception evidence requires status='exception'")

        if self.status == "http_status":
            if self.http_status is None:
                raise ValueError("http_status outcomes require an HTTP status code")
        elif self.http_status is not None:
            raise ValueError("an HTTP status code requires status='http_status'")

        if self.integrity_error and self.status != "exception":
            raise ValueError("integrity_error requires status='exception'")

        return self


class CellResult(BaseModel):
    """Outcomes collected from one matrix cell."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cell: Cell
    outcomes: list[Outcome]

    @model_validator(mode="after")
    def validate_unique_scenarios(self) -> Self:
        """Reject ambiguous duplicate scenario IDs within a cell."""
        seen: set[str] = set()
        for outcome in self.outcomes:
            if outcome.scenario_id in seen:
                raise ValueError(
                    f"duplicate scenario_id in cell {self.cell.value}: "
                    f"{outcome.scenario_id}"
                )
            seen.add(outcome.scenario_id)

        return self


class DivergenceCategory(StrEnum):
    CRASH = "crash"
    INTEGRITY_ERROR = "integrity_error"
    STATUS_CHANGE = "status_change"
    BODY_CHANGE = "body_change"


class ComparisonPolicy(BaseModel):
    """State which response values are required to remain equivalent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    compare_body: bool = Field(default=False, strict=True)
    volatile_fields: frozenset[str] = frozenset()


class CandidateDivergence(BaseModel):
    """An observed old-code regression awaiting independent reproduction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cell: Cell
    scenario_id: str = Field(min_length=1)
    category: DivergenceCategory
    baseline: Outcome
    observed: Outcome
    policy: ComparisonPolicy = Field(default_factory=ComparisonPolicy)

    @model_validator(mode="after")
    def validate_comparison_identity(self) -> Self:
        """Require matching checks and an old-code skew cell."""
        if self.cell not in {Cell.A0D1, Cell.A0D1W}:
            raise ValueError("deploy comparisons require A0D1 or A0D1w")
        if not (
            self.scenario_id == self.baseline.scenario_id == self.observed.scenario_id
        ):
            raise ValueError("comparison evidence must have matching scenario IDs")
        return self


class ComparisonIssueKind(StrEnum):
    BASELINE_FAILED = "baseline_failed"
    PROBE_INPUT_UNAVAILABLE = "probe_input_unavailable"
    MISSING_CHECK = "missing_check"
    UNEXPECTED_CHECK = "unexpected_check"
    NEW_CODE_FAILURE = "new_code_failure"
    ROLLBACK_SETUP_FAILED = "rollback_setup_failed"


class ComparisonIssue(BaseModel):
    """Unavailable coverage or a health problem, distinct from a skew candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cell: Cell
    kind: ComparisonIssueKind
    message: str = Field(min_length=1)
    scenario_id: str | None = None
    outcome: Outcome | None = None


class ComparisonResult(BaseModel):
    """Candidate evidence and issues; absence of candidates is not a safety proof."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidates: list[CandidateDivergence] = Field(
        default_factory=list[CandidateDivergence]
    )
    issues: list[ComparisonIssue] = Field(default_factory=list[ComparisonIssue])
