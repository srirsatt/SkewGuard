"""Validated request descriptions for Django HTTP scenario execution."""

from typing import Literal, Self

from pydantic import (
    BaseModel, 
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from skewguard.exec.types import ComparisonPolicy

type HttpMethod = Literal["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"]
type ScenarioKind = Literal["read", "write"]


class Scenario(BaseModel):
    """A local HTTP request with optional authentication and response selection.

    Response selections name top-level JSON object keys. An empty selection asks
    for execution/status checks without requiring response body equality.
    """

    # essentially validating my model run + HTTP formatting for correctness here

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    scenario_id: str = Field(pattern=r"^http:[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    method: HttpMethod
    path: str = Field(min_length=1)
    kind: ScenarioKind
    body: JsonValue = None
    as_user: str | None = Field(default=None, min_length=1, pattern=r"\S")
    compare_fields: list[str] = Field(default_factory=list)
    # agent scenario fields

    def comparison_policy(self) -> ComparisonPolicy:
        """Use status checks by default and body comparison for selected keys."""
        return ComparisonPolicy(
            compare_body=bool(self.compare_fields),
            compare_fields=tuple(self.compare_fields),
        )

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if not value.startswith("/") or value.startswith("//"):
            # invalid http path start
            raise ValueError("path must start with one slash and stay local")
        
        invalid_character = any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
        if "#" in value or invalid_character:
            raise ValueError("path can't contain fragments or unencoded whitespace")

        # valid path
        return value

    @field_validator("compare_fields")
    @classmethod
    def validate_compare_fields(cls, value: list[str]) -> list[str]:
        if any(not name.strip() for name in value) or len(set(value)) != len(value):
            raise ValueError("comparison fields must be nonblank and unique")
        return value

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        read_method = self.method in {"GET", "HEAD", "OPTIONS"}

        if (self.kind == "read") != read_method:
            raise ValueError("kind must agree with HTTP method")
        
        if read_method and self.body is not None:
            raise ValueError("read requests must not contain a body")

        return self

    
