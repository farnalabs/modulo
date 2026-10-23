"""Error-tracking list/filter indexes; drop the unused events.level index.

Revision ID: 0326_error_tracking_lookup_indexes
Revises: 0325_run_seam_coverage
Create Date: 2026-10-23

Schema legs (all index-only; no column/constraint change):

1. ``ix_error_groups_org_last_seen`` — ``(organisation_id, last_seen)``.
   The error-groups list (``get_error_groups`` in ``db/crud/error_tracking``,
   served by ``GET /api/v1/errors`` and the instance-scope twin) has NO
   default status filter: it is ``WHERE organisation_id = $1 ORDER BY
   last_seen DESC LIMIT $2``. The only existing indexes are
   ``ix_error_groups_organisation_id`` (organisation_id) and the FAR-748
   ``ix_error_groups_org_status_last_seen`` (organisation_id, status,
   last_seen). Neither can return rows in ``last_seen`` order for an
   unfiltered org, so the planner sorts the org's whole group set (or walks
   the heap) before applying the LIMIT. This is the endpoint's default view,
   hit on every page load.

2. ``ix_error_groups_org_level_peak_last_seen`` —
   ``(organisation_id, level_peak, last_seen)``. ``level`` is a first-class
   list filter (``GET /api/v1/errors?level=...``, exposed in
   ``AdminErrorsView``). With only the org-wide last_seen index the planner
   must scan in ``last_seen`` order and discard rows until the LIMIT is
   met — fine for the common ``error`` level, but a ``critical`` triage
   filter scans the org's entire group set. A backward scan of this index
   satisfies both the equality and the descending order.

3. ``ix_error_events_org_source_fingerprint`` /
   ``ix_error_events_org_environment_fingerprint`` —
   ``(organisation_id, source, fingerprint)`` and
   ``(organisation_id, environment, fingerprint)``. The ``source``,
   ``environment`` and ``search`` list filters are applied to the
   *events* table as a fingerprint semi-join:

       ErrorGroup.fingerprint.in_(
           select(ErrorEvent.fingerprint).where(
               ErrorEvent.organisation_id == org_id,
               ErrorEvent.source == source,          # or environment
           )
       )

   ``error_events`` is append-only and never purged (only an organisation
   cascade removes rows), so without a leading-column index on those
   filter columns Postgres reads every event the org has ever recorded and
   rechecks the predicate on the heap. ``fingerprint`` is included as the
   third column so the semi-join is index-only. ``source`` is the
   user-facing filter (the admin errors view); ``environment`` is the same
   query shape on the documented API parameter.

4. ``ix_error_events_org_level`` is DROPPED. Migration 0185 added it for
   "org-scoped level filtering", but no statement anywhere filters
   ``error_events`` by ``level``: the level filter is applied to
   ``error_groups.level_peak`` (see leg 2) and the only Python-side level
   comparison (``api/routes/errors.py``, the frontend-ingest downgrade) runs
   after the rows are loaded. It is a write-only cost on the append-only,
   high-insert-volume events table.

No CONCURRENTLY: this repo's migration runner executes inside a
transaction (the 0155/0185/0292 index migrations use plain ``create_index``
for the same reason). The locks are ordinary ``CREATE INDEX`` share locks,
held for the duration of the build.

Downgrade restores the 0185 index and drops the four added ones.
"""

from __future__ import annotations

from alembic import op

revision: str = "0326_error_tracking_lookup_indexes"
down_revision: str | None = "0325_run_seam_coverage"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_error_groups_org_last_seen",
        "error_groups",
        ["organisation_id", "last_seen"],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        "ix_error_groups_org_level_peak_last_seen",
        "error_groups",
        ["organisation_id", "level_peak", "last_seen"],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        "ix_error_events_org_source_fingerprint",
        "error_events",
        ["organisation_id", "source", "fingerprint"],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        "ix_error_events_org_environment_fingerprint",
        "error_events",
        ["organisation_id", "environment", "fingerprint"],
        unique=False,
        if_not_exists=True,
    )
    op.drop_index("ix_error_events_org_level", table_name="error_events", if_exists=True)


def downgrade() -> None:
    op.create_index(
        "ix_error_events_org_level",
        "error_events",
        ["organisation_id", "level", "created_at"],
        unique=False,
        if_not_exists=True,
    )
    op.drop_index("ix_error_events_org_environment_fingerprint", table_name="error_events", if_exists=True)
    op.drop_index("ix_error_events_org_source_fingerprint", table_name="error_events", if_exists=True)
    op.drop_index("ix_error_groups_org_level_peak_last_seen", table_name="error_groups", if_exists=True)
    op.drop_index("ix_error_groups_org_last_seen", table_name="error_groups", if_exists=True)
