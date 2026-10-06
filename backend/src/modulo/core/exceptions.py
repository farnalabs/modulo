"""Core-level exception types for Modulo."""

import uuid


class SnapshotLockNotAvailableError(Exception):
    """Raised when the pipeline snapshot lock cannot be acquired immediately."""


class TriggersPausedError(RuntimeError):
    """Raised when a trigger-initiated run is attempted while the org is paused.

    The org-wide "pause all pipeline triggers" kill-switch. Raised by the
    ``create_run`` gate and by the pre-flight checks in the webhook engine and
    the cron/polling fire jobs. ``trigger_id``, ``org_id``, and ``trigger_type``
    are all optional so every call site can supply whatever context it holds.

    NOTE: this is the ONLY exception the pause feature introduces. Read
    failures are NEVER converted to this error — a SQLAlchemyError read failure
    in the org-pause check propagates untouched (never fabricate "paused").
    """

    def __init__(
        self,
        *,
        trigger_id: uuid.UUID | None = None,
        org_id: uuid.UUID | None = None,
        trigger_type: str | None = None,
    ) -> None:
        self.trigger_id = trigger_id
        self.org_id = org_id
        self.trigger_type = trigger_type
        super().__init__(f"Triggers paused for org {org_id}")


class OrgDeletedError(RuntimeError):
    """Raised by ``create_run`` when the target organisation is soft-deleted or missing.

    The soft-deleted-org guard refuses to create a run in an org whose deletion
    flow has set status='deleted' (or in a hard-deleted org — no row). A domain
    exception (not ``ValueError``) so API routes and cron/trigger callers can map
    it to a structured 4xx (409 for a deleted org, 404 for a missing org) instead
    of a generic 500. Lives in ``modulo.core.exceptions`` alongside
    ``TriggersPausedError``; the ``db-does-not-import-core`` contract exempts the
    consuming CRUD module (see ``.importlinter``).
    """

    def __init__(self, *, org_id: uuid.UUID | None = None, deleted: bool = True) -> None:
        self.org_id = org_id
        self.deleted = deleted
        super().__init__(f"cannot create run: organisation {org_id} is {'deleted' if deleted else 'missing'}")


class RateLimitConflictError(RuntimeError):
    """Raised by ``create_run`` when the rate-limit unique constraint fires.

    The partial unique index ``uq_runs_pipeline_rate_limit_key`` on
    ``(pipeline_id, rate_limit_key) WHERE rate_limit_key IS NOT NULL`` catches
    concurrent creates that both pass the windowed count check. The CRUD layer
    translates the ``IntegrityError`` to this domain exception so callers
    (trigger engine, routes) can map it to a structured rate-limit response
    without importing database-layer internals.
    """

    def __init__(
        self,
        *,
        pipeline_id: uuid.UUID | None = None,
        rate_limit_key: str | None = None,
    ) -> None:
        self.pipeline_id = pipeline_id
        self.rate_limit_key = rate_limit_key
        super().__init__(f"rate limit conflict for pipeline {pipeline_id}, key {rate_limit_key!r}")


class PipelineNotRunnableError(RuntimeError):
    """Raised by ``create_run`` when the pipeline's lifecycle state refuses a run (FAR-1528).

    The pipeline-state gate sits in ``create_run`` — the SINGLE choke point
    every run origin converges on (REST manual trigger + rerun, MCP
    ``trigger_pipeline``, webhook, replay, cron, polling, agent_signal, slack
    app-mention, variant runs, feedback correction) — so one gate covers them
    all. ``state`` is the refusing lifecycle condition:

    * ``"archived"`` — ``pipelines.archived_at IS NOT NULL``
    * ``"deleted"``  — ``pipelines.deleted_at IS NOT NULL`` (soft delete)

    Sibling of ``TriggersPausedError`` (the org-wide pause kill-switch) and
    ``OrgDeletedError`` (the org-level guard), but distinct from both: this is
    a PER-PIPELINE refusal and is never mapped to the trigger ``skipped`` /
    ``PAUSE_SKIP_REASON`` envelope. Read failures are NEVER converted into
    this error — a ``SQLAlchemyError`` read failure in the gate propagates
    untouched (fail closed by raising the read error itself, never fabricate
    a state and never treat an unreadable row as runnable).

    FAR-1530 (per-pipeline Paused state) adds another ``state`` value at the
    same gate; callers mapping this error respond with 409 Conflict and the
    state in the detail for any state they do not special-case.

    Lives in ``modulo.core.exceptions`` alongside ``TriggersPausedError``; the
    ``db-does-not-import-core`` contract exempts the consuming CRUD module
    (see ``.importlinter``).
    """

    def __init__(self, *, state: str, pipeline_id: uuid.UUID | None = None) -> None:
        self.state = state
        self.pipeline_id = pipeline_id
        super().__init__(f"cannot create run: pipeline {pipeline_id} is {state}")
