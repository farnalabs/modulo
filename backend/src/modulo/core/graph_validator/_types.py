"""Shared types for graph validation."""

import uuid
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Literal, TypeVar

_T = TypeVar("_T")


def try_parse_uuid(raw: object) -> uuid.UUID | None:
    """Parse a UUID from an arbitrary value, returning None on failure."""
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError, AttributeError):
        return None


def try_parse_uuids(raw_values: Collection[object]) -> tuple[set[uuid.UUID], list[object]]:
    """Parse UUIDs from a collection, returning (valid_uuids, invalid_values)."""
    valid: set[uuid.UUID] = set()
    invalid: list[object] = []
    for v in raw_values:
        parsed = try_parse_uuid(v)
        if parsed is None:
            invalid.append(v)
        else:
            valid.add(parsed)
    return valid, invalid


@dataclass
class ValidationIssue:
    severity: Literal["error", "warning"]
    code: str
    message: str
    node_id: str | None = None


@dataclass
class ValidationResult:
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    def error(self, code: str, message: str, node_id: str | None = None) -> None:
        self.issues.append(ValidationIssue("error", code, message, node_id))

    def warning(self, code: str, message: str, node_id: str | None = None) -> None:
        self.issues.append(ValidationIssue("warning", code, message, node_id))
