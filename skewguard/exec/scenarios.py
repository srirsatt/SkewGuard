"""Validated request descriptions for Django HTTP scenario execution."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

type HttpMethod = Literal["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"]
type ScenarioKind = Literal["read", "write"]


class Scenario(BaseModel):
    """A local HTTP request with optional authentication and response selection.

    Response selections name top-level JSON object keys. An empty selection asks
    for execution/status checks without requiring response body equality.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
