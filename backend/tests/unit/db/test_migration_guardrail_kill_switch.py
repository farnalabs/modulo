"""Kill-switch flag migration ownership + up/down round-trip (FAR-223 item 13.5).

The ``organisations.guardrails_kill_switch`` (and its ``guardrails_kill_switch_at``
companion) columns are owned by the schema-reconciliation migration
``0108_schema_org_identity``, NOT by a dedicated guardrail migration. Per the
squash convention (AGENTS.md: "reconciliation is not reversible in general"),
0108 uses guarded ``ADD COLUMN IF NOT EXISTS`` DDL so it brings any database to
the current schema state without assuming history, and its DOWNGRADE is a no-op
(a reconciliation migration must not drop columns a pre-squash DB already has).

These tests assert:

* the kill-switch column is ADDED by 0108's upgrade (``ADD COLUMN IF NOT EXISTS
  "guardrails_kill_switch"``),
* the companion ``guardrails_kill_switch_at`` column and the
  ``ck_organisations_guardrails_kill_switch_at`` CHECK constraint are present,
* the DOWNGRADE is a deliberate no-op for the kill-switch column (guarded —
  it does not ``DROP COLUMN`` it), so an up/down round-trip through 0108
  preserves the column rather than destroying it.
"""

from pathlib import Path

from alembic.script import ScriptDirectory

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"

_MIGRATION_0006 = "0108_schema_org_identity"
_HEAD_MIGRATION = "0113_guardrail_summary"


def _source(name: str) -> str:
    path = _VERSIONS / f"{name}.py"
    assert path.exists(), f"Migration file missing: {path}"
    return path.read_text(encoding="utf-8")


def _script() -> ScriptDirectory:
    return ScriptDirectory(str(_VERSIONS.parent))


class TestKillSwitchMigration:
    def test_single_head_is_0113(self) -> None:
        heads = _script().get_heads()
        assert heads == [_HEAD_MIGRATION], f"expected a single head, got {heads}"

    def test_0006_upgrade_adds_kill_switch_columns(self) -> None:
        source = _source(_MIGRATION_0006)
        # Upgrade: the kill-switch flag + timestamp are added via guarded DDL.
        assert 'ADD COLUMN IF NOT EXISTS "guardrails_kill_switch" boolean DEFAULT false' in source
        assert 'ADD COLUMN IF NOT EXISTS "guardrails_kill_switch_at" timestamp with time zone' in source

    def test_0006_creates_kill_switch_at_check(self) -> None:
        source = _source(_MIGRATION_0006)
        assert "ck_organisations_guardrails_kill_switch_at" in source
        assert "NOT guardrails_kill_switch" in source  # enabled => at IS NOT NULL

    def test_0006_downgrade_is_noop_for_kill_switch(self) -> None:
        source = _source(_MIGRATION_0006)
        # The downgrade must NOT drop the kill-switch column: a reconciliation
        # migration is not reversible in general, and a pre-squash DB may hold
        # the column. An up/down round-trip therefore preserves it.
        assert 'DROP COLUMN "guardrails_kill_switch"' not in source
        assert 'DROP COLUMN "guardrails_kill_switch_at"' not in source

    def test_kill_switch_owned_by_0108_not_0113(self) -> None:
        # The kill-switch flag shipped in PR A's reconciliation (0108), not in
        # PR B's head migration (0113, which only adds runs.guardrail_summary_json).
        source_0113 = _source(_HEAD_MIGRATION)
        assert "guardrails_kill_switch" not in source_0113
        assert "guardrail_summary_json" in source_0113
