"""Version EvalDefinition/EvalSuite + record version on EvalResult (FAR-382).

Revision ID: 0138_eval_versioning
Revises: 0137_eval_suite_run
Create Date: 2026-08-25

Versioning so regression comparison is version-scoped: a v1 -> v2 rubric change
(an edit to an eval definition's config) must NEVER look like a regression. The
suite-run baseline already snapshots the ``definition_checksum`` (config
fingerprint) at creation, so a config change produces a new baseline tuple. This
migration adds the EXPLICIT version signal to complement that checksum.

What this migration does (additive, reversible, self-contained):

* ``eval_definitions.version`` — integer, non-null from cutover, ``server_default``
  ``'1'``. Bumped on every create/update so a rubric change is a version-scoped
  event. Existing rows are backfilled to ``1`` by the default.
* ``eval_definitions.pre_version_raw`` — JSON, nullable. A snapshot of the raw
  definition config as it existed BEFORE the current version was stamped, so a
  reversal is reconstructable. Backfilled for pre-existing rows from their
  current ``config_json``.
* ``eval_suites.version`` / ``eval_suites.pre_version_raw`` — mirrors the eval
  definition pair (``pre_version_raw`` snapshots ``eval_definition_ids`` for a
  pre-existing suite).
* ``eval_results.eval_definition_version`` — integer, nullable. A snapshot of the
  eval-definition ``version`` that scored the result at write time. NULL for
  legacy rows written before versioning — such rows are resolved against the
  definition's current (latest) version at read time (the NULL-version lookup).

No RLS / ownership ceremony is required: this only ALTERs existing tables, so
every step runs as the migration caller (the same role that already ALTERs
``eval_results`` in 0137). No new tables, no new FK constraints, no new indexes.

Reversible: downgrade drops the three added columns, restoring the pre-versioning
schema exactly.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0138_eval_versioning"
down_revision: str | None = "0137_eval_suite_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # eval_definitions: version + pre-version config snapshot.
    op.add_column("eval_definitions", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("eval_definitions", sa.Column("pre_version_raw", sa.JSON(), nullable=True))
    op.execute(
        "UPDATE eval_definitions "
        "SET pre_version_raw = json_build_object('config_json', config_json) "
        "WHERE pre_version_raw IS NULL"
    )

    # eval_suites: version + pre-version suite snapshot.
    op.add_column("eval_suites", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("eval_suites", sa.Column("pre_version_raw", sa.JSON(), nullable=True))
    op.execute(
        "UPDATE eval_suites "
        "SET pre_version_raw = json_build_object('eval_definition_ids', eval_definition_ids) "
        "WHERE pre_version_raw IS NULL"
    )

    # eval_results: the eval-definition version snapshot that scored each result.
    op.add_column("eval_results", sa.Column("eval_definition_version", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("eval_results", "eval_definition_version")
    op.drop_column("eval_suites", "pre_version_raw")
    op.drop_column("eval_suites", "version")
    op.drop_column("eval_definitions", "pre_version_raw")
    op.drop_column("eval_definitions", "version")
