# system prompt for seed generation - data inserts for use in testing

import json
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
)

SEED_SYSTEM_PROMPT = """\
Propose one row using field names and valid values from the old release’s model definition.
Follow the GET route to its view and choose values that make the view’s query return that row.
"""

# tells the seed agent how to shape model response

class SeedProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(
        description="Django model label, such as catalog.Product",
        pattern=r"^[A-Za-z_]\w*\.[A-Za-z_]\w*$",
    )
    fields: dict[str, JsonValue]

    @field_validator("fields")
    @classmethod
    def require_portable_json(
        cls, fields: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        """Keep nested NaN and infinity from changing to null in a saved seed."""
        json.dumps(fields, allow_nan=False)
        return fields


class SeedDraft(BaseModel):
    """One saved model turn awaiting execution on a disposable baseline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str
    reply: str
    proposal: SeedProposal | None = None
    parse_error: str | None = None

    @classmethod
    def parse_reply(cls, scenario_id: str, reply: str) -> "SeedDraft":
        try:
            proposal = SeedProposal.model_validate_json(reply)
        except ValidationError as error:
            issues = error.errors(include_url=False, include_input=False)
            return cls(
                scenario_id=scenario_id, reply=reply,
                parse_error=f"Invalid seed proposal: {issues}"[:800],
            )
        return cls(scenario_id=scenario_id, reply=reply, proposal=proposal)


class SeedAttempt(BaseModel):
    """Qualification of a seed row for a particular GET request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str
    status: Literal["accepted", "rejected"]
    detail: str = Field(min_length=1)
    proposal: SeedProposal | None = None
