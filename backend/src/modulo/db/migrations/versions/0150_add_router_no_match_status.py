"""runs — add ``router_no_match`` to the ``ck_runs_status`` CHECK constraint.

Revision ID: 0150_add_router_no_match_status
Revises: 0149_suite_run_trigger_kind
Create Date: 2026-08-24

FAR-402 P1 (FAR-415) introduces the ``router_no_match`` terminal run status
emitted when a Router node (F2-A) has no matching rule and no ``default``. The
``ck_runs_status`` CHECK constraint enumerates the allowed status values, so
the new status must be added to it on existing databases. A rolling-deploy
mitigation (design doc Appendix B, O1): the constraint change is additive and
backward-compatible — pre-existing rows never carry the new status, and once
this migration lands the backend may emit it.

The constraint is recreated idempotently: only drop + recreate when the
current definition does not already include ``router_no_match`` (mirrors the
guard pattern used throughout 0110).

Renumber note: this migration was originally ``0138_add_router_no_match_status``
but collided with main's ``0138_eval_versioning``. It is renumbered to
``0139`` and re-parented onto main's head. It was first re-parented onto
``0141_pipeline_edge_ports``; after main advanced to ``0143_rest_connector_profile``
(FAR-412 REST connector profile) the down-revision was updated to ``0143``. After
main advanced again to ``0144_broaden_notification_status_in_app`` (FAR-436) the
down-revision was updated to ``0144`` and then to ``0147_json_to_jsonb_standardize``
after main advanced through 0145/0146/0147 and then ``0148_pipeline_snapshot_versioning_far420``
(FAR-402 P6) landed on main. It was renumbered a second time to ``0150`` and
re-parented onto ``0149_suite_run_trigger_kind`` (FAR-377, main's head) after
``0149`` landed on main, so the graph stays a single linear chain with
``0150`` chained off ``0149_suite_run_trigger_kind`` as the tip. The recreated
constraint list MUST include ``cost_ceiling_exceeded`` (added by 0146) as well as
``router_no_match`` so this migration does not clobber main's status addition.
"""

from __future__ import annotations

from alembic import op

revision: str = "0150_add_router_no_match_status"
down_revision: str | None = "0149_suite_run_trigger_kind"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_UPGRADE_SQL = (
    "DO $$ BEGIN "
    "IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_status' "
    "AND regexp_replace(pg_get_constraintdef(oid), '\\s+', '', 'g') "
    "NOT LIKE '%router_no_match%') THEN "
    "ALTER TABLE public.runs DROP CONSTRAINT IF EXISTS ck_runs_status; "
    "END IF; "
    "IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_status') THEN "
    "ALTER TABLE public.runs ADD CONSTRAINT ck_runs_status "
    "CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, "
    "'running'::character varying, 'awaiting_human'::character varying, "
    "'claimed'::character varying, 'complete'::character varying, "
    "'failed'::character varying, 'cancelled'::character varying, "
    "'eval_failed'::character varying, 'stalled'::character varying, "
    "'budget_exceeded'::character varying, 'cost_ceiling_exceeded'::character varying, 'router_no_match'::character varying])::text[]))); "
    "END IF; "
    "END $$;"
)

_DOWNGRADE_SQL = (
    "DO $$ BEGIN "
    "IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_status') THEN "
    "ALTER TABLE public.runs DROP CONSTRAINT ck_runs_status; "
    "END IF; "
    "ALTER TABLE public.runs ADD CONSTRAINT ck_runs_status "
    "CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, "
    "'running'::character varying, 'awaiting_human'::character varying, "
    "'claimed'::character varying, 'complete'::character varying, "
    "'failed'::character varying, 'cancelled'::character varying, "
    "'eval_failed'::character varying, 'stalled'::character varying, "
    "'budget_exceeded'::character varying, 'cost_ceiling_exceeded'::character varying])::text[]))); "
    "END $$;"
)


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
