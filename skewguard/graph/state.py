"""Validated inputs and phase results shared by the verification graph."""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from skewguard.agents.scenario import ScenarioAttempt
from skewguard.agents.seed import SeedAttempt, SeedDraft
from skewguard.config import RunConfig
from skewguard.exec.types import VerificationResult
from skewguard.report.json_report import RunReport
from skewguard.static.types import StaticContext


class RunState(BaseModel):
    """Record one run after preparation has pinned both release commits."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    config: RunConfig
    settings_module: str = Field(min_length=1)
    base_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    head_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    seed_fixture: str | None = Field(default=None, min_length=1)

    static_context: StaticContext | None = None
    proposal_reply: str = ""
    scenario_attempts: tuple[ScenarioAttempt, ...] = ()
    scenario_pending: bool = False
    seed_draft: SeedDraft | None = None
    seed_attempts: tuple[SeedAttempt, ...] = ()
    verification: VerificationResult | None = None
    report: RunReport | None = None

    @property
    def remaining_model_calls(self) -> int:
        """Include the scenario reply awaiting qualification while seeds are tried."""
        spent = len(self.scenario_attempts) + len(self.seed_attempts)
        return self.config.max_llm_calls - spent - int(self.scenario_pending)

    @field_validator("settings_module")
    @classmethod
    def validate_settings_module(cls, value: str) -> str:
        """Require an importable module-name shape before application execution."""
        if not all(part.isidentifier() for part in value.split(".")):
            raise ValueError("settings_module must be a dotted Python module name")
        return value
