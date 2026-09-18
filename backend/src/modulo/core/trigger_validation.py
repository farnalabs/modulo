"""Shared ``ongoing`` trigger configuration validation (FAR-158).

The ``ongoing`` trigger type keeps a pipeline topped up to a target number of
in-flight (active or queued) runs. Its configuration is validated IDENTICALLY
at every write surface:

* REST ``POST /pipelines/{id}/triggers`` + ``PUT /triggers/{id}``
  (``modulo.api.routes.triggers``)
* MCP ``create_trigger`` / ``update_trigger`` (``modulo.api.mcp_server``)
* ``PATCH /triggers/{id}/ongoing`` (via the same helper)

Sharing the validator here (rather than importing from ``api.routes.triggers``)
avoids an api -> core import cycle from ``mcp_server``. Non-ongoing trigger
types pass through unvalidated.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import HTTPException


def validate_ongoing_config(
    trigger_type: str,
    *,
    max_concurrent_runs: int,
    daily_spend_limit: Decimal | float | None,
    config_json: dict[str, Any] | None,
    pipeline_max_concurrent_runs: int,
) -> None:
    """Validate an ongoing trigger's configuration; raise ``HTTPException(422)`` on failure.

    Rules (FAR-158, DB-enforced by migration 0086):

    * ``daily_spend_limit`` is REQUIRED and must be > 0 — an unbounded daemon
      is a runaway cost hazard.
    * target ``max_concurrent_runs`` must be within 1..20 (the partial CHECK
      ``ck_triggers_ongoing_target_range``).
    * target must NOT exceed the owning pipeline's ``max_concurrent_runs`` —
      the effective pool is ``min(trigger, pipeline)`` at top-up time, so a
      trigger target above the pipeline cap is silently useless (rejected up
      front rather than misconfigured).
    * ``config_json.scan_interval_seconds`` must be >= 60 (the scheduler tick
      is 60s; a lower value would be ignored).

    Any failure — including a non-numeric ``daily_spend_limit`` or
    ``scan_interval_seconds`` (which would otherwise leak a ``TypeError`` /
    ``ValueError`` from the comparison / ``int()`` coercion as an HTTP 500) —
    raises ``HTTPException(422)``. A non-ongoing ``trigger_type`` passes
    through with no checks.
    """
    if trigger_type != "ongoing":
        return

    try:
        spend_limit_ok = daily_spend_limit is not None and daily_spend_limit > 0
    except TypeError:
        spend_limit_ok = False
    if not spend_limit_ok:
        raise HTTPException(
            status_code=422,
            detail="ongoing triggers require daily_spend_limit (must be greater than 0)",
        )
    if not 1 <= max_concurrent_runs <= 20:
        raise HTTPException(
            status_code=422,
            detail="ongoing trigger target max_concurrent_runs must be between 1 and 20",
        )
    if max_concurrent_runs > pipeline_max_concurrent_runs:
        raise HTTPException(
            status_code=422,
            detail=(
                "ongoing trigger target max_concurrent_runs cannot exceed the pipeline's "
                f"max_concurrent_runs ({pipeline_max_concurrent_runs})"
            ),
        )
    try:
        scan_interval = int((config_json or {}).get("scan_interval_seconds") or 60)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=422,
            detail="ongoing trigger scan_interval_seconds must be an integer (number of seconds)",
        ) from None
    if scan_interval < 60:
        raise HTTPException(
            status_code=422,
            detail="ongoing trigger scan_interval_seconds must be at least 60",
        )
