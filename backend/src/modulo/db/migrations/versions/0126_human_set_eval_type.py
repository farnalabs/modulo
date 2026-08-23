"""Allow the `human_set` eval type.

Revision ID: 0126_human_set_eval_type
Revises: 0125_soft_delete_lookup_indexes
Create Date: 2026-08-23

The ``ck_eval_definitions_type`` check constraint on ``eval_definitions``
currently whitelists five eval types. FAR-393 adds ``human_set`` — a
deterministic, versioned, human-authored eval set — as a first-class,
selectable eval type. This drops and recreates the constraint to include it.

``human_set`` is the trustworthy correctness path: it is not model-mediated
(unlike ``llm_judge``) and asserts semantics rather than just shape (unlike
``regex`` / ``json_schema``). See ``modulo.core.eval_engine.human_eval_sets``.
"""

from alembic import op
from sqlalchemy import text

revision = "0126_human_set_eval_type"
down_revision = "0125_soft_delete_lookup_indexes"
branch_labels = None
depends_on = None

_OLD_CHECK = (
    "eval_type::text = ANY (ARRAY["
    "'llm_judge'::character varying, "
    "'regex'::character varying, "
    "'json_schema'::character varying, "
    "'custom_function'::character varying, "
    "'guardrail'::character varying"
    "]::text[])"
)
_NEW_CHECK = (
    "eval_type::text = ANY (ARRAY["
    "'llm_judge'::character varying, "
    "'regex'::character varying, "
    "'json_schema'::character varying, "
    "'custom_function'::character varying, "
    "'guardrail'::character varying, "
    "'human_set'::character varying"
    "]::text[])"
)


def upgrade() -> None:
    op.execute(text("ALTER TABLE public.eval_definitions DROP CONSTRAINT IF EXISTS ck_eval_definitions_type"))
    op.execute(
        text(f"ALTER TABLE public.eval_definitions ADD CONSTRAINT ck_eval_definitions_type CHECK ({_NEW_CHECK})")
    )


def downgrade() -> None:
    op.execute(text("ALTER TABLE public.eval_definitions DROP CONSTRAINT IF EXISTS ck_eval_definitions_type"))
    op.execute(
        text(f"ALTER TABLE public.eval_definitions ADD CONSTRAINT ck_eval_definitions_type CHECK ({_OLD_CHECK})")
    )
