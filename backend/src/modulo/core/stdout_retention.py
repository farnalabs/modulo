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
