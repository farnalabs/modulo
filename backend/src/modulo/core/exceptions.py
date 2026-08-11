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
        super().__init__(f"cannot create run: organisation {org_id} is " + ("deleted" if deleted else "missing"))
