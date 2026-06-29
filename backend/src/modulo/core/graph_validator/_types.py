"""Shared types for graph validation."""

from dataclasses import dataclass, field


@dataclass
class ValidationIssue:
    severity: str  # "error" | "warning"
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
