"""Promote EvalDefinition.suite_id into a first-class EvalSuite entity (FAR-374 Phase 1).

Revision ID: 0130_eval_suite_entity
Revises: 0125_soft_delete_lookup_indexes
Create Date: 2026-08-23

Phase 1 of the Generic Eval Product MVP. Today ``EvalDefinition.suite_id`` is a
free-text string tag that groups evals. This migration promotes it into a real
``eval_suites`` table so later phases (EvalDataset / EvalCase, then SuiteRun +
comparison / regression) have a stable grouping anchor.

What this migration does (additive, reversible, self-contained):

1. Creates ``eval_suites`` (OrgScoped, with ``owner_team_id`` + ``visibility``
   team-scoping like model_backends). After creation the table is re-owned to the
   ``modulo_migrate`` role (guarded on the role existing) so the runtime
   ``modulo_app`` role — which is NOT the owner — can only read it through RLS.
   The DDL itself runs as the migration's connection role, which is ``modulo_migrate``
   in production (DATABASE_ADMIN_URL) and the bootstrap superuser in test
   containers; either way it has the privileges to reference ``teams`` /
   ``organisations`` for the FK columns.
2. Adds ``eval_definitions.eval_suite_id`` FK -> ``eval_suites.id``
   (ON DELETE SET NULL — deleting a suite must never cascade-delete defs).
3. Backfills: one ``EvalSuite`` per DISTINCT (organisation_id, suite_id) where
   suite_id IS NOT NULL, carrying the original string in ``legacy_suite_id`` and
   the member eval_definitions in ``eval_definition_ids``. NULL suite_id values
   get ONE per-organisation sentinel suite (reserved legacy_suite_id
   ``__NO_SUITE__``), never a shared row. The backfill is set-based and bounded
   per organisation; it touches ONLY ``eval_definitions`` (never ``eval_results``)
   so it cannot hot-block eval writes. It is idempotent via the ``UNIQUE(
   organisation_id, legacy_suite_id)`` constraint and a ``WHERE eval_suite_id IS
   NULL`` guard, so re-running it is a no-op.
4. Enables + FORCEs Row Level Security on ``eval_suites`` and creates the
   org-isolation policy ``rls_org_isolation`` (identical to every other
   OrgScoped table). The OrgScoped mixin alone is NOT sufficient — the policy
   is required so the runtime ``modulo_app`` role (non-owner) is filtered.

``evaluate_suite()`` in the eval engine is intentionally NOT touched — it still
resolves the legacy ``suite_id`` string internally, so all existing production
call sites are byte-for-byte unchanged.

Downgrade drops the policy, the FK column, and the table, restoring the
pre-migration schema exactly.
"""

from alembic import op
from sqlalchemy import text

revision = "0130_eval_suite_entity"
down_revision = "0129_runs_json_to_jsonb"
branch_labels = None
depends_on = None

# Reserved legacy_suite_id for eval definitions that had a NULL legacy suite_id
# (one sentinel EvalSuite per organisation, enforced by the unique constraint).
_SENTINEL_LEGACY_SUITE_ID = "__NO_SUITE__"

_ORG_ISOLATION_POLICY = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"

# Ownership is transferred to the migration role after DDL so the runtime
# modulo_app role (non-owner) is filtered by RLS. Guarded on the role existing.
_OWNER_TRANSFER_SQL = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'modulo_migrate') "
    "THEN ALTER TABLE public.eval_suites OWNER TO modulo_migrate; END IF; END $$;"
)


def _transfer_ownership() -> None:
    op.execute(text(_OWNER_TRANSFER_SQL))


# The backfill DO block — the only interpolated value is the reserved sentinel
# constant, embedded at module load (not an f-string, not user input).
_BACKFILL_SQL = """
DO $$
DECLARE
    org_rec RECORD;
    suite_rec RECORD;
    new_suite_id UUID;
BEGIN
    FOR org_rec IN
        SELECT DISTINCT organisation_id FROM eval_definitions
    LOOP
        -- Non-null legacy suite_id tags -> one suite each.
        FOR suite_rec IN
            SELECT organisation_id, suite_id
            FROM eval_definitions
            WHERE organisation_id = org_rec.organisation_id
              AND suite_id IS NOT NULL
            GROUP BY organisation_id, suite_id
        LOOP
            INSERT INTO eval_suites (
                id, organisation_id, owner_team_id, visibility,
                name, description, legacy_suite_id,
                eval_definition_ids, input_set_ref,
                created_at, updated_at
            )
            SELECT
                gen_random_uuid(),
                suite_rec.organisation_id,
                NULL,
                'org',
                left(suite_rec.suite_id, 255),
                'Backfilled from legacy eval_definitions.suite_id (FAR-374).',
                suite_rec.suite_id,
                COALESCE((
                    SELECT json_agg(ed.id ORDER BY ed.id)
                    FROM eval_definitions ed
                    WHERE ed.organisation_id = suite_rec.organisation_id
                      AND ed.suite_id = suite_rec.suite_id
                      AND ed.deleted_at IS NULL
                ), '[]'::json),
                NULL,
                now(),
                now()
            WHERE NOT EXISTS (
                SELECT 1 FROM eval_suites s
                WHERE s.organisation_id = suite_rec.organisation_id
                  AND s.legacy_suite_id = suite_rec.suite_id
            )
            RETURNING id INTO new_suite_id;

            UPDATE eval_definitions
            SET eval_suite_id = new_suite_id
            WHERE organisation_id = suite_rec.organisation_id
              AND suite_id = suite_rec.suite_id
              AND eval_suite_id IS NULL;
        END LOOP;

        -- Sentinel suite for NULL legacy suite_id (one per org).
        INSERT INTO eval_suites (
            id, organisation_id, owner_team_id, visibility,
            name, description, legacy_suite_id,
            eval_definition_ids, input_set_ref,
            created_at, updated_at
        )
        SELECT
            gen_random_uuid(),
            org_rec.organisation_id,
            NULL,
            'org',
            '__NO_SUITE__',
            'Eval definitions with no legacy suite_id (FAR-374).',
            '__NO_SUITE__',
            COALESCE((
                SELECT json_agg(ed.id ORDER BY ed.id)
                FROM eval_definitions ed
                WHERE ed.organisation_id = org_rec.organisation_id
                  AND ed.suite_id IS NULL
                  AND ed.deleted_at IS NULL
            ), '[]'::json),
            NULL,
            now(),
            now()
        WHERE EXISTS (
            SELECT 1 FROM eval_definitions ed
            WHERE ed.organisation_id = org_rec.organisation_id
              AND ed.suite_id IS NULL
        )
        AND NOT EXISTS (
            SELECT 1 FROM eval_suites s
            WHERE s.organisation_id = org_rec.organisation_id
              AND s.legacy_suite_id = '__NO_SUITE__'
        )
        RETURNING id INTO new_suite_id;

        UPDATE eval_definitions
        SET eval_suite_id = new_suite_id
        WHERE organisation_id = org_rec.organisation_id
          AND suite_id IS NULL
          AND eval_suite_id IS NULL;
    END LOOP;
END $$;
"""


def upgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return

    # 1. Create the eval_suites table (owns references to teams/organisations).
    op.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS eval_suites (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                organisation_id UUID NOT NULL
                    REFERENCES organisations(id) ON DELETE CASCADE,
                owner_team_id UUID REFERENCES teams(id) ON DELETE RESTRICT,
                visibility VARCHAR(10) NOT NULL DEFAULT 'org',
                name VARCHAR(255) NOT NULL,
                description VARCHAR(2000),
                eval_definition_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
                input_set_ref VARCHAR(255),
                legacy_suite_id VARCHAR(255),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT ck_eval_suites_visibility
                    CHECK (visibility IN ('org', 'team')),
                CONSTRAINT ck_eval_suites_team_owner
                    CHECK (visibility = 'org' OR owner_team_id IS NOT NULL),
                CONSTRAINT uq_eval_suites_org_legacy_suite_id
                    UNIQUE (organisation_id, legacy_suite_id)
            )
            """
        )
    )

    op.execute(text("CREATE INDEX IF NOT EXISTS ix_eval_suites_organisation_id ON eval_suites (organisation_id)"))

    # 2. Add the FK column + constraint on eval_definitions.
    op.execute(text("ALTER TABLE eval_definitions ADD COLUMN IF NOT EXISTS eval_suite_id UUID"))
    op.execute(
        text(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM pg_constraint "
            "WHERE conname = 'fk_eval_definitions_eval_suite_id') THEN "
            "ALTER TABLE eval_definitions "
            "ADD CONSTRAINT fk_eval_definitions_eval_suite_id "
            "FOREIGN KEY (eval_suite_id) REFERENCES eval_suites(id) ON DELETE SET NULL; "
            "END IF; END $$;"
        )
    )

    # 3. Idempotent backfill (bounded per organisation; touches only
    #    eval_definitions, never eval_results).
    op.execute(text(_BACKFILL_SQL))

    # 4. Row Level Security (org isolation) on the new table.
    op.execute(text('ALTER TABLE "eval_suites" ENABLE ROW LEVEL SECURITY'))
    op.execute(text('ALTER TABLE "eval_suites" FORCE ROW LEVEL SECURITY'))
    op.execute(text('DROP POLICY IF EXISTS rls_org_isolation ON "eval_suites"'))
    op.execute(text('CREATE POLICY rls_org_isolation ON "eval_suites" USING (' + _ORG_ISOLATION_POLICY + ")"))

    # Ensure the runtime modulo_app role is a non-owner (filtered by RLS).
    _transfer_ownership()


def downgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return

    op.execute(text('DROP POLICY IF EXISTS rls_org_isolation ON "eval_suites"'))
    op.execute(
        text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM pg_constraint "
            "WHERE conname = 'fk_eval_definitions_eval_suite_id') THEN "
            "ALTER TABLE eval_definitions DROP CONSTRAINT fk_eval_definitions_eval_suite_id; "
            "END IF; END $$;"
        )
    )
    op.execute(text("ALTER TABLE eval_definitions DROP COLUMN IF EXISTS eval_suite_id"))
    op.execute(text("DROP TABLE IF EXISTS eval_suites"))
