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

    Sort dictionary keys, retain all keys and list ordering/cardinality, and
    replace only named scalar fields using mask_volatile_value. Recurse into
    dictionaries/lists even when their parent key is declared volatile. Preserve
    all other values and never mutate the input. Default to no masked fields.
    """
    raise NotImplementedError("JSON outcome normalization is not implemented")


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
