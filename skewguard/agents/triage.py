"""Structured explanations for reproduced migration failures."""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from skewguard.exec.types import DivergenceCategory, ReproductionDecision

TRIAGE_SYSTEM_PROMPT = """\
Explain why the old request failed after the migration. Use the migration operation
to identify what changed. Use the before-and-after test results to explain the
failure, and make sure to cite the old-code line that depends on the changed
field.
"""


class CodeCitation(BaseModel):
    """An old-release source location with a one-based line number."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    path: str = Field(
        min_length=1, description="File path relative to the old checkout."
    )
    line: int = Field(ge=1, strict=True)


class TriageExplanation(BaseModel):
    """The model's attribution for one divergence already confirmed by execution."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    cause_operation: str | None = Field(
        min_length=1, description="Django operation name from the evidence, or null."
    )
    migration_file: str | None = Field(
        min_length=1, description="Supplied head-release migration path, or null."
    )
    code_location: CodeCitation | None = Field(
        description="Old-release path and line showing the dependency. Use null "
        "when the supplied source does not show it."
    )
    category: DivergenceCategory = Field(description="Copy the recorded category.")
    explanation: str = Field(min_length=1)
    confidence: float = Field(
        ge=0, le=1, strict=True, allow_inf_nan=False,
        description="Model confidence in this explanation, from 0 to 1.",
    )

    @classmethod
    def from_finding(cls, reply: str, finding: ReproductionDecision) -> Self:
        """Check the model's category against the reproduced failure."""
        if finding.status != "confirmed":
            raise ValueError("triage requires a confirmed failure")
        result = cls.model_validate_json(reply)
        expected = finding.candidate.category
        if result.category != expected:
            raise ValueError(
                f"Model returned {result.category.value} but execution recorded "
                f"{expected.value}"
            )
        return result
