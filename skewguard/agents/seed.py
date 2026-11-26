# system prompt for seed generation - data inserts for use in testing

from pydantic import BaseModel, ConfigDict, Field, JsonValue

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