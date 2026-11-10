"""Shared stdout/stderr retention validation + coercion helpers (FAR-792).

Both the API ``PipelineGraphNode`` and the CLI ``ApplyGraphNode`` enforce the
identical positive-integer gate on ``stdout_max_bytes``. The logic lives here
so it is defined exactly once (no drift-prone duplicated validators in two
models) and so ``node_runner`` can reuse the same error message constant.
"""

from __future__ import annotations

from typing import Any

from pydantic import field_validator

_STDOUT_MAX_BYTES_ERROR_MSG = "stdout_max_bytes must be a positive integer"
_STDOUT_RETENTION_CONFIG_ERROR_MSG = (
    "stdout_retention_config must be null or a dict with 'mode' in ('tail', 'full') "
    "and optional 'max_bytes' as a positive integer"
)


def validate_stdout_max_bytes(v: Any) -> int | None:
    """Save-time positive-integer gate for ``stdout_max_bytes``.

    Reject bools (``isinstance(bool, int)``), non-positive, non-integer and
    non-finite values so a smuggled value can never raise the retention cap.
    Ints are taken exactly (no float coercion) so values above 2**53 do not
    lose precision. ``None`` passes through.
    """
    if v is None:
        return v
    if isinstance(v, bool):
        raise ValueError(_STDOUT_MAX_BYTES_ERROR_MSG)
    try:
        if isinstance(v, int):
            value = v
        else:
            f = float(v)
            if not f.is_integer():
                raise ValueError
            value = int(f)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(_STDOUT_MAX_BYTES_ERROR_MSG) from None
    if value <= 0:
        raise ValueError(_STDOUT_MAX_BYTES_ERROR_MSG)
    return value


def validate_stdout_retention_config(v: Any) -> dict[str, Any] | None:
    """Save-time validation for pipeline-level ``stdout_retention_config``.

    Shape: ``{"mode": "tail"|"full", "max_bytes": <positive int>}`` or None.
    None means no pipeline override. ``max_bytes`` is optional (defaults to
    the code-level full-mode default when absent). An empty dict ``{}`` is the
    documented "clear" operation and normalizes to None (no pipeline override).
    """
    if v is None:
        return None
    if not isinstance(v, dict):
        raise ValueError(_STDOUT_RETENTION_CONFIG_ERROR_MSG)
    if not v:
        # Empty dict is the documented clear operation — normalize to None so a
        # client following the field/schema description gets a successful clear
        # instead of a 422 for the missing 'mode' key.
        return None
    mode = v.get("mode")
    if mode not in ("tail", "full"):
        raise ValueError(_STDOUT_RETENTION_CONFIG_ERROR_MSG)
    result: dict[str, Any] = {"mode": mode}
    max_bytes = v.get("max_bytes")
    if max_bytes is not None:
        result["max_bytes"] = validate_stdout_max_bytes(max_bytes)
    return result


class StdoutRetentionValidatorMixin:
    """Provides the ``stdout_max_bytes`` field validator for node models.

    Inherited by both ``PipelineGraphNode`` (API) and ``ApplyGraphNode`` (CLI)
    so the save-time gate is defined in exactly one place. ``check_fields=False``
    lets the validator live on this field-less mixin and bind to the field that
    each concrete model declares.
    """

    @field_validator("stdout_max_bytes", mode="before", check_fields=False)
    @classmethod
    def _validate_stdout_max_bytes(cls, v: Any) -> Any:
        return validate_stdout_max_bytes(v)
