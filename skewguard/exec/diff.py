"""Normalize copies of observed evidence for deterministic comparison."""

from pydantic import JsonValue

from skewguard.exec.types import Outcome

VOLATILE_MARKER = "<volatile>"


def mask_volatile_value(value: JsonValue) -> JsonValue:
    """Mask scalar identifiers/times while preserving nulls and boolean values."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (str, int, float)):
        return VOLATILE_MARKER
    return value


def normalize_value(
    value: JsonValue,
    *,
    volatile_fields: frozenset[str] = frozenset(),
) -> JsonValue:
    """Return a recursively copied JSON value with declared volatile leaves masked.

    """
    if isinstance(value, dict):
        normalized: dict[str, JsonValue] = {}

        for key, item in sorted(value.items()):
            if key in volatile_fields and not isinstance(item, (dict, list)):
                normalized[key] = mask_volatile_value(item)
            else:
                normalized[key] = normalize_value(item, volatile_fields=volatile_fields)

        return normalized

    if isinstance(value, list):
        return [
            normalize_value(item, volatile_fields=volatile_fields)
            for item in value
        ]

    return value

def normalize_outcome(
    outcome: Outcome,
    *,
    volatile_fields: frozenset[str] = frozenset(),
) -> Outcome:
    """Copy body evidence while retaining scenario IDs, statuses, and exceptions."""
    return outcome.model_copy(
        update={
            "body": normalize_value(outcome.body, volatile_fields=volatile_fields),
        }
    )
