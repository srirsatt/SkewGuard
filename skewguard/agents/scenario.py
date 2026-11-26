# what can an agent propose in a scenario?

from pydantic import BaseModel, ConfigDict, Field # for verification

from skewguard.exec.scenarios import Scenario

class ScenarioProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario: Scenario | None
    reason: str = Field(min_length=1)

# agent instructions!! for http scenario generation

SCENARIO_SYSTEM_PROMPT = """\
Propose one GET or POST request that exercises the affected model or field.
Ground the path, method, and request data in the supplied old-release routes
and view code. Explain which code connects the request to the change.
Choose compare_fields only from top-level keys visible in the response.
If the supplied code shows no relevant HTTP endpoint, return a null scenario
and explain what evidence is missing. Use the migration to decide which data to exercise, 
and use the old release's routes and view or serializer code to choose the request
and top-level response keys to compare.
"""