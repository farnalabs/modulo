"""Typed-judge write errors and shared dialects constants (ADR 066 D4/D8).

Kept separate from :mod:`~modulo.core.eval_engine.typed_judge.config` so the
legacy-freeze module and the schema module can share one error vocabulary
without importing each other (the write chokepoint needs both).

Every code below is surfaced verbatim as the RFC 9457 ``code`` extension
member by ``eval_definition_write.create_or_update_eval`` (a typed 422).
"""

from __future__ import annotations

import re
from typing import Any

#: The dialect marker on a typed judge config.
TYPED_ANSWER_CONTRACT = "typed_questions"

#: Stable 422 codes. ``judge_config_legacy_read_only`` is the ADR-named one.
CODE_TYPED_REQUIRED = "judge_config_typed_required"
CODE_LEGACY_READ_ONLY = "judge_config_legacy_read_only"
CODE_INVALID = "judge_config_invalid"
CODE_VERDICT_KIND = "judge_verdict_kind_unsupported"
#: Resolution failures for a ``model_backend`` backing (ADR 066 D3/D16).
CODE_BACKEND_NOT_FOUND = "judge_backend_not_found"
CODE_BACKEND_NOT_ORG_OWNED = "judge_backend_not_org_owned"
CODE_BACKEND_NOT_ACTIVE = "judge_backend_not_active"

#: Remediation hint attached to every legacy-freeze rejection.
MIGRATE_HINT = (
    "Re-send the definition with a typed config_json (answer_contract: "
    '"typed_questions", a non-empty "field" or judge_whole_output: true, a '
    '"questions" list, a "backing" and a "pass_rule") to migrate it in place, or '
    "delete and recreate it. A legacy config cannot be edited as legacy; no "
    "backfill is performed because it would change every definition checksum and "
    "suite baseline (ADR 066 D8)."
)

#: Any key that could carry a credential. The typed dialect has no credential
#: field at all (credentials live in the vault / model-backend rows, D16), so a
#: match is a rejection, not a redaction.
CREDENTIAL_KEY_PATTERN = re.compile(
    r"(api[_-]?key|token|secret|password|passwd|credential|bearer|private[_-]?key)",
    re.IGNORECASE,
)


class JudgeConfigWriteError(ValueError):
    """A judge write was rejected with a stable code and a remediation hint."""

    def __init__(self, code: str, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint or MIGRATE_HINT

    def detail(self) -> dict[str, str]:
        """The 422 ``detail`` payload: code, message and remediation hint."""
        return {"code": self.code, "message": self.message, "hint": self.hint}


def invalid(message: str, *, hint: str | None = None) -> JudgeConfigWriteError:
    """Build the generic schema-rejection error (``judge_config_invalid``)."""
    return JudgeConfigWriteError(CODE_INVALID, message, hint=hint or "Fix config_json and retry.")


def reject_credential_keys(obj: Any, where: str) -> None:
    """Reject any credential-like key anywhere in *obj* (ADR 066 D16).

    Recursive because a pasted secret is equally dangerous nested inside an
    option description or a level object; the dialect has no credential field,
    so every match is rejected rather than redacted.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and CREDENTIAL_KEY_PATTERN.search(key):
                raise invalid(
                    f"{where} carries a credential-like key {key!r}; credentials belong in the "
                    "model backend / vault, never in config_json (ADR 066 D16)"
                )
            reject_credential_keys(value, f"{where}.{key}")
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            reject_credential_keys(value, f"{where}[{index}]")


__all__ = [
    "CODE_BACKEND_NOT_ACTIVE",
    "CODE_BACKEND_NOT_FOUND",
    "CODE_BACKEND_NOT_ORG_OWNED",
    "CODE_INVALID",
    "CODE_LEGACY_READ_ONLY",
    "CODE_TYPED_REQUIRED",
    "CODE_VERDICT_KIND",
    "CREDENTIAL_KEY_PATTERN",
    "MIGRATE_HINT",
    "TYPED_ANSWER_CONTRACT",
    "JudgeConfigWriteError",
    "invalid",
    "reject_credential_keys",
]
