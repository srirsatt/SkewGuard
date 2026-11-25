"""Specify HTTP scenario transport and request classification constraints."""

import pytest
from pydantic import ValidationError

from skewguard.exec.scenarios import Scenario


@pytest.fixture
def request_spec() -> dict[str, object]:
    return {
        "scenario_id": "http:catalog.products:list",
        "method": "GET",
        "path": "/products/",
        "kind": "read",
    }


@pytest.mark.parametrize(
    ("method", "kind"),
    [
        ("GET", "read"),
        ("HEAD", "read"),
        ("OPTIONS", "read"),
        ("POST", "write"),
        ("PUT", "write"),
        ("PATCH", "write"),
        ("DELETE", "write"),
    ],
)
def test_request_methods_have_explicit_read_write_classification(
    request_spec: dict[str, object], method: str, kind: str
) -> None:
    spec = {**request_spec, "method": method, "kind": kind}
    if kind == "write":
        spec["body"] = {"name": "Keyboard", "price": 80}

    saved = Scenario.model_validate(spec).model_dump(mode="json")

    assert saved["method"] == method
    assert saved["kind"] == kind
    assert saved["body"] == spec.get("body")


def test_request_survives_json_without_altering_business_values(
    request_spec: dict[str, object],
) -> None:
    body = {"name": "  Keyboard  ", "price": 80, "options": [True, None, ""]}
    spec = {
        **request_spec,
        "scenario_id": "http:catalog.products:create",
        "method": "POST",
        "kind": "write",
        "body": body,
        "as_user": "buyer@example.test",
        "compare_fields": ["name", "price"],
    }
    scenario = Scenario.model_validate(spec)

    restored = Scenario.model_validate_json(scenario.model_dump_json())

    assert restored == scenario
    assert restored.model_dump() == spec


def test_anonymous_request_defaults_to_status_checks(
    request_spec: dict[str, object],
) -> None:
    saved = Scenario.model_validate(request_spec).model_dump()

    assert saved["body"] is None
    assert saved["as_user"] is None
    assert saved["compare_fields"] == []


def test_request_keeps_encoded_query_parameters(
    request_spec: dict[str, object],
) -> None:
    path = "/products/?q=mechanical%20keyboard&page=2"

    saved = Scenario.model_validate({**request_spec, "path": path}).model_dump()

    assert saved["path"] == path


@pytest.mark.parametrize(
    "patch",
    [
        {"method": "TRACE"},
        {"method": "get"},
        {"kind": "update"},
        {"method": "POST", "kind": "read"},
        {"method": "GET", "kind": "write"},
        {"method": "HEAD", "kind": "write"},
        {"method": "OPTIONS", "kind": "write"},
        {"scenario_id": ""},
        {"scenario_id": "orm:catalog.Product:read"},
        {"scenario_id": "http:"},
        {"scenario_id": "http:products:list\n"},
        {"path": ""},
        {"path": "products/"},
        {"path": "https://example.invalid/products/"},
        {"path": "//example.invalid/products/"},
        {"path": "/products/#price"},
        {"path": "/products/#"},
        {"path": "/products/?q=hello world"},
        {"path": "/products/\n"},
        {"path": "/products/\x00"},
        {"path": "/products/\x7f"},
        {"as_user": ""},
        {"as_user": "   "},
        {"compare_fields": ["price", "price"]},
        {"compare_fields": [""]},
        {"compare_fields": ["   "]},
        {"python": "print('unsupported request field')"},
        {"method": "POST", "kind": "write", "body": object()},
    ],
)
def test_request_rejects_unexecutable_or_ambiguous_descriptions(
    request_spec: dict[str, object], patch: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        Scenario.model_validate({**request_spec, **patch})


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
@pytest.mark.parametrize("body", [{}, [], "", 0, False])
def test_read_requests_reject_even_empty_or_false_bodies(
    request_spec: dict[str, object], method: str, body: object
) -> None:
    with pytest.raises(ValidationError):
        Scenario.model_validate({**request_spec, "method": method, "body": body})


@pytest.mark.parametrize("field", ["scenario_id", "method", "path", "kind"])
def test_request_requires_an_identity_and_execution_instructions(
    request_spec: dict[str, object], field: str
) -> None:
    spec = dict(request_spec)
    del spec[field]

    with pytest.raises(ValidationError):
        Scenario.model_validate(spec)
