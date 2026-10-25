"""Legacy judge dialect: readable and executable, never creatable (ADR 066 D8).

Before ADR 066 a judge definition was "a prompt in a free-form ``config_json``":
``{field, model_backend_id, instructions...}`` with no dialect marker. Those
rows stay readable and execute exactly as they do today, but they are frozen:

* **Create** requires the typed dialect — ``answer_contract ==
  "typed_questions"`` plus a structurally valid question set (the schema lives
  in :mod:`~modulo.core.eval_engine.typed_judge.config`). A legacy config
  cannot be created at any of the eight write paths.
* **Update** accepts a legacy config only when it is byte-identical to the
  stored one (canonical JSON comparison). Editing a legacy config is a typed
  422 carrying ``judge_config_legacy_read_only`` and a migration hint.
* **No backfill.** Rewriting a legacy config would change every definition
  checksum and every suite baseline, and would make historical
  ``eval_definition_version`` lie.
* **Residual execution is counted** (``eval_judge.legacy_rows_count``) so the
  read path can be removed when the count reaches zero (hard backstop
  2027-03-31).

This module is API-framework free: it raises :class:`JudgeConfigWriteError`
with a stable ``code``; the HTTP boundary (``eval_definition_write``) translates
it into a 422.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from modulo.core.eval_engine.typed_judge.config import (  # re-exported: S1 import surface
    validate_typed_judge_config,
)
from modulo.core.eval_engine.typed_judge.errors import (
    CODE_INVALID,
    CODE_LEGACY_READ_ONLY,
    CODE_TYPED_REQUIRED,
    CODE_VERDICT_KIND,
    MIGRATE_HINT,
    TYPED_ANSWER_CONTRACT,
    JudgeConfigWriteError,
    invalid,
)

_log = logging.getLogger(__name__)


_legacy_counter: Any = None


def _get_legacy_counter() -> Any:
    """Lazily create the process-wide legacy-execution counter (idempotent)."""
    global _legacy_counter
    if _legacy_counter is None:
        try:
            from opentelemetry import metrics

            meter = metrics.get_meter_provider().get_meter("modulo.eval_judge", version="0.1.0")
            _legacy_counter = meter.create_counter(
                name="modulo_eval_judge_legacy_rows_count",
                description="Executions of a legacy (pre-ADR-066) judge definition",
                unit="1",
            )
        except Exception:
            # Metrics are best-effort; the structured log below is the durable
            # signal and must never be skipped because OTel is unavailable.
            return None
    return _legacy_counter


def record_legacy_execution(*, eval_id: Any, eval_name: str = "") -> None:
    """Count one execution of a legacy-dialect judge definition."""
    _log.warning(
        "eval_judge.legacy_rows_count",
        extra={"eval_id": str(eval_id), "eval_name": eval_name, "legacy_rows_count": 1},
    )
    counter = _get_legacy_counter()
    if counter is not None:
        try:
            counter.add(1, {"eval_type": "model_judge"})
        except Exception:
            _log.warning("eval_judge.legacy_counter_unavailable", exc_info=True)


def is_typed_judge_config(config: Any) -> bool:
    """True when *config* declares the typed-questions dialect."""
    return isinstance(config, dict) and config.get("answer_contract") == TYPED_ANSWER_CONTRACT


def is_legacy_judge_config(config: Any) -> bool:
    """True when *config* is a judge config with no dialect marker."""
    return isinstance(config, dict) and config.get("answer_contract") != TYPED_ANSWER_CONTRACT


def canonical_json(config: Any) -> str:
    """Canonical serialisation used for the legacy byte-identical check."""
    return json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)


def reject_legacy_judge_write(
    *,
    config_json: Any,
    stored_config: Any = None,
    stored_eval_type: str | None = None,
    is_update: bool = False,
    new_eval_type: str = "model_judge",
) -> None:
    """Enforce the create-required / update-immutable legacy rule.

    ``stored_config`` / ``stored_eval_type`` describe the persisted row on an
    update (both ``None`` on a create). A legacy **judge** row may be re-sent
    byte-for-byte (a metadata edit) — including when the update changes the
    eval type away from ``model_judge``, because replacing its config would
    silently discard a frozen judge without a migration. Any write that lands
    ``model_judge`` must carry the typed dialect.
    """
    legacy_judge_row = is_update and stored_eval_type == "model_judge" and is_legacy_judge_config(stored_config)
    if legacy_judge_row:
        # The one legal in-place change is MIGRATING to the typed dialect. A
        # legacy-to-legacy edit is refused; a typed replacement is the repair
        # the remediation hint names.
        if is_typed_judge_config(config_json):
            validate_typed_judge_config(config_json)
            return
        if canonical_json(config_json) != canonical_json(stored_config):
            raise JudgeConfigWriteError(
                CODE_LEGACY_READ_ONLY,
                "this model_judge definition still uses the legacy (pre-ADR-066) "
                "config dialect, which is read-only as legacy",
            )
        return
    if new_eval_type == "model_judge":
        validate_typed_judge_config(config_json)


__all__ = [
    "CODE_INVALID",
    "CODE_LEGACY_READ_ONLY",
    "CODE_TYPED_REQUIRED",
    "CODE_VERDICT_KIND",
    "MIGRATE_HINT",
    "TYPED_ANSWER_CONTRACT",
    "JudgeConfigWriteError",
    "canonical_json",
    "invalid",
    "is_legacy_judge_config",
    "is_typed_judge_config",
    "record_legacy_execution",
    "reject_legacy_judge_write",
    "validate_typed_judge_config",
]
