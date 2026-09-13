"""Unit tests for workspace_input_audit (FAR-801).

Covers:
* ``WorkspaceInputAuditRecord.to_audit_dict`` — URL redaction, secret stripping.
* ``record_resolved_inputs`` — idempotent upsert, best-effort failure.
* ``record_drift`` — drift detection, no-drift persistence, best-effort failure.
* ``redact_url`` — public URL redaction helper.

Uses an in-memory SQLite engine with only the ``run_node_outputs`` table
(no ORM tenant-filter listener, no RLS).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.pipeline_engine.workspace_input_audit import (
    AUDIT_NODE_ID,
    WorkspaceInputAuditRecord,
    record_drift,
    record_resolved_inputs,
    redact_url,
)
from modulo.db.models.base import Base
from modulo.db.models.run_node_outputs import RunNodeOutput

_TABLE_NAMES = {"run_node_outputs", "organisations"}

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
_RUN_ID = uuid.UUID("01234567-89ab-cdef-0123-456789abcdef")


def _make_record(
    *,
    input_name: str = "my-repo",
    url: str = "https://user:pass@github.com/acme/my-repo.git",
    resolved_sha: str = "a" * 40,
    final_sha: str | None = None,
    drift_detected: bool = False,
    status: str = "resolved",
) -> WorkspaceInputAuditRecord:
    return WorkspaceInputAuditRecord(
        input_name=input_name,
        connector_instance_id=None,
        host="github.com",
        url_redacted=url,
        requested_ref_kind="branch",
        requested_ref_value="main",
        resolved_sha=resolved_sha,
        final_sha=final_sha,
        drift_detected=drift_detected,
        dest="/home/user/my-repo",
        status=status,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        tables = [t for t in Base.metadata.sorted_tables if t.name in _TABLE_NAMES]
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess


# ---------------------------------------------------------------------------
# WorkspaceInputAuditRecord.to_audit_dict — URL redaction
# ---------------------------------------------------------------------------


class TestToAuditDict:
    """Verify that to_audit_dict strips userinfo and never leaks secrets."""

    def test_strips_userinfo_from_https_url(self) -> None:
        rec = _make_record(url="https://user:pass@github.com/acme/repo.git")
        d = rec.to_audit_dict()
        assert "user" not in d["url_redacted"]
        assert "pass" not in d["url_redacted"]
        assert d["url_redacted"] == "https://github.com/acme/repo.git"

    def test_strips_userinfo_user_only(self) -> None:
        rec = _make_record(url="https://token@github.com/acme/repo.git")
        d = rec.to_audit_dict()
        assert d["url_redacted"] == "https://github.com/acme/repo.git"

    def test_preserves_url_without_userinfo(self) -> None:
        rec = _make_record(url="https://github.com/acme/repo.git")
        d = rec.to_audit_dict()
        assert d["url_redacted"] == "https://github.com/acme/repo.git"

    def test_preserves_git_at_url(self) -> None:
        rec = _make_record(url="git@github.com:acme/repo.git")
        d = rec.to_audit_dict()
        assert d["url_redacted"] == "git@github.com:acme/repo.git"

    def test_all_expected_keys_present(self) -> None:
        rec = _make_record()
        d = rec.to_audit_dict()
        expected_keys = {
            "input_name",
            "connector_instance_id",
            "host",
            "url_redacted",
            "requested_ref_kind",
            "requested_ref_value",
            "resolved_sha",
            "final_sha",
            "drift_detected",
            "dest",
            "status",
        }
        assert set(d.keys()) == expected_keys

    def test_no_credential_fields_in_dict(self) -> None:
        rec = _make_record(url="https://ghp_abc123@github.com/acme/repo.git")
        d = rec.to_audit_dict()
        serialised = str(d)
        assert "ghp_abc123" not in serialised
        assert "pass" not in serialised


# ---------------------------------------------------------------------------
# redact_url — public helper
# ---------------------------------------------------------------------------


class TestRedactUrl:
    def test_strips_userinfo(self) -> None:
        assert redact_url("https://u:p@host/x") == "https://host/x"

    def test_noop_when_clean(self) -> None:
        assert redact_url("https://host/x") == "https://host/x"

    def test_git_at_preserved(self) -> None:
        assert redact_url("git@host:path") == "git@host:path"


# ---------------------------------------------------------------------------
# WorkspaceInputAuditRecord — validation
# ---------------------------------------------------------------------------


class TestRecordValidation:
    def test_valid_statuses(self) -> None:
        for status in ("resolved", "provisioned", "failed"):
            rec = _make_record(status=status)
            assert rec.status == status

    def test_invalid_status_raises(self) -> None:
        with pytest.raises(ValueError, match="not valid"):
            _make_record(status="bogus")


# ---------------------------------------------------------------------------
# record_resolved_inputs — idempotent upsert
# ---------------------------------------------------------------------------


class TestRecordResolvedInputs:
    @pytest.mark.anyio
    async def test_writes_audit_row(self, session: AsyncSession) -> None:
        rec = _make_record()
        await record_resolved_inputs(
            session,
            run_id=_RUN_ID,
            organisation_id=_ORG,
            node_id="sandbox-1",
            attempt_key="attempt-0",
            records=[rec],
            status="resolved",
        )
        await session.flush()

        from sqlalchemy import select

        row = (
            await session.execute(
                select(RunNodeOutput).where(
                    RunNodeOutput.run_id == _RUN_ID,
                    RunNodeOutput.node_id == AUDIT_NODE_ID,
                    RunNodeOutput.attempt_key == "attempt-0",
                )
            )
        ).scalar_one()
        assert isinstance(row.outputs_json, dict)
        payload: dict[str, Any] = row.outputs_json
        assert "workspace_inputs" in payload
        assert len(payload["workspace_inputs"]) == 1
        assert payload["workspace_inputs"][0]["input_name"] == "my-repo"
        assert payload["status"] == "resolved"
        assert payload["resolved_for_node_id"] == "sandbox-1"

    @pytest.mark.anyio
    async def test_same_attempt_updates(self, session: AsyncSession) -> None:
        """Idempotent: calling twice for same attempt_key overwrites."""
        rec1 = _make_record(input_name="first")
        await record_resolved_inputs(
            session,
            run_id=_RUN_ID,
            organisation_id=_ORG,
            node_id="sandbox-1",
            attempt_key="attempt-0",
            records=[rec1],
            status="resolved",
        )
        await session.flush()

        rec2 = _make_record(input_name="second")
        await record_resolved_inputs(
            session,
            run_id=_RUN_ID,
            organisation_id=_ORG,
            node_id="sandbox-1",
            attempt_key="attempt-0",
            records=[rec2],
            status="resolved",
        )
        await session.flush()

        from sqlalchemy import select

        row = (
            await session.execute(
                select(RunNodeOutput).where(
                    RunNodeOutput.run_id == _RUN_ID,
                    RunNodeOutput.node_id == AUDIT_NODE_ID,
                    RunNodeOutput.attempt_key == "attempt-0",
                )
            )
        ).scalar_one()
        payload = row.outputs_json
        assert len(payload["workspace_inputs"]) == 1
        assert payload["workspace_inputs"][0]["input_name"] == "second"

    @pytest.mark.anyio
    async def test_different_attempts_are_distinct(self, session: AsyncSession) -> None:
        """Different attempt_keys produce different rows."""
        rec = _make_record()
        await record_resolved_inputs(
            session,
            run_id=_RUN_ID,
            organisation_id=_ORG,
            node_id="sandbox-1",
            attempt_key="attempt-0",
            records=[rec],
            status="resolved",
        )
        await record_resolved_inputs(
            session,
            run_id=_RUN_ID,
            organisation_id=_ORG,
            node_id="sandbox-1",
            attempt_key="attempt-1",
            records=[rec],
            status="resolved",
        )
        await session.flush()

        from sqlalchemy import select

        rows = list(
            (
                await session.execute(
                    select(RunNodeOutput).where(
                        RunNodeOutput.run_id == _RUN_ID,
                        RunNodeOutput.node_id == AUDIT_NODE_ID,
                    )
                )
            ).scalars()
        )
        assert len(rows) == 2
        attempt_keys = {r.attempt_key for r in rows}
        assert attempt_keys == {"attempt-0", "attempt-1"}


# ---------------------------------------------------------------------------
# record_resolved_inputs — best-effort failure
# ---------------------------------------------------------------------------


class TestRecordResolvedInputsBestEffort:
    @pytest.mark.anyio
    async def test_failure_is_swallowed(self, session: AsyncSession, caplog: pytest.LogCaptureFixture) -> None:
        """A failing session write is logged, not raised."""
        rec = _make_record()
        with patch(
            "modulo.core.pipeline_engine.workspace_input_audit.dialect_insert",
            side_effect=RuntimeError("db down"),
        ):
            # Must NOT raise.
            await record_resolved_inputs(
                session,
                run_id=_RUN_ID,
                organisation_id=_ORG,
                node_id="sandbox-1",
                attempt_key="attempt-0",
                records=[rec],
                status="resolved",
            )
        assert "record_resolved_inputs failed" in caplog.text
        assert "swallowed" in caplog.text


# ---------------------------------------------------------------------------
# record_drift — drift detection
# ---------------------------------------------------------------------------


class TestRecordDrift:
    @pytest.mark.anyio
    async def test_drift_true_when_sha_differs(self, session: AsyncSession) -> None:
        rec = _make_record(resolved_sha="a" * 40)
        await record_resolved_inputs(
            session,
            run_id=_RUN_ID,
            organisation_id=_ORG,
            node_id="sandbox-1",
            attempt_key="attempt-0",
            records=[rec],
            status="resolved",
        )
        await session.flush()

        await record_drift(
            session,
            run_id=_RUN_ID,
            organisation_id=_ORG,
            node_id="sandbox-1",
            attempt_key="attempt-0",
            final_shas={"my-repo": "b" * 40},
        )
        await session.flush()

        from sqlalchemy import select

        row = (
            await session.execute(
                select(RunNodeOutput).where(
                    RunNodeOutput.run_id == _RUN_ID,
                    RunNodeOutput.node_id == AUDIT_NODE_ID,
                    RunNodeOutput.attempt_key == "attempt-0",
                )
            )
        ).scalar_one()
        entry = row.outputs_json["workspace_inputs"][0]
        assert entry["final_sha"] == "b" * 40
        assert entry["drift_detected"] is True

    @pytest.mark.anyio
    async def test_drift_false_when_sha_matches(self, session: AsyncSession) -> None:
        rec = _make_record(resolved_sha="a" * 40)
        await record_resolved_inputs(
            session,
            run_id=_RUN_ID,
            organisation_id=_ORG,
            node_id="sandbox-1",
            attempt_key="attempt-0",
            records=[rec],
            status="resolved",
        )
        await session.flush()

        await record_drift(
            session,
            run_id=_RUN_ID,
            organisation_id=_ORG,
            node_id="sandbox-1",
            attempt_key="attempt-0",
            final_shas={"my-repo": "a" * 40},
        )
        await session.flush()

        from sqlalchemy import select

        row = (
            await session.execute(
                select(RunNodeOutput).where(
                    RunNodeOutput.run_id == _RUN_ID,
                    RunNodeOutput.node_id == AUDIT_NODE_ID,
                    RunNodeOutput.attempt_key == "attempt-0",
                )
            )
        ).scalar_one()
        entry = row.outputs_json["workspace_inputs"][0]
        assert entry["final_sha"] == "a" * 40
        assert entry["drift_detected"] is False

    @pytest.mark.anyio
    async def test_no_prior_record_is_noop(self, session: AsyncSession, caplog: pytest.LogCaptureFixture) -> None:
        """record_drift when no audit record exists logs a warning, no-op."""
        await record_drift(
            session,
            run_id=_RUN_ID,
            organisation_id=_ORG,
            node_id="sandbox-1",
            attempt_key="attempt-0",
            final_shas={"my-repo": "a" * 40},
        )
        assert "no audit record found" in caplog.text


# ---------------------------------------------------------------------------
# record_drift — best-effort failure
# ---------------------------------------------------------------------------


class TestRecordDriftBestEffort:
    @pytest.mark.anyio
    async def test_failure_is_swallowed(self, session: AsyncSession, caplog: pytest.LogCaptureFixture) -> None:
        """A failing session write is logged, not raised."""
        rec = _make_record(resolved_sha="a" * 40)
        await record_resolved_inputs(
            session,
            run_id=_RUN_ID,
            organisation_id=_ORG,
            node_id="sandbox-1",
            attempt_key="attempt-0",
            records=[rec],
            status="resolved",
        )
        await session.flush()

        with patch(
            "modulo.core.pipeline_engine.workspace_input_audit.dialect_insert",
            side_effect=RuntimeError("db down"),
        ):
            # Must NOT raise.
            await record_drift(
                session,
                run_id=_RUN_ID,
                organisation_id=_ORG,
                node_id="sandbox-1",
                attempt_key="attempt-0",
                final_shas={"my-repo": "b" * 40},
            )
        assert "record_drift failed" in caplog.text
        assert "swallowed" in caplog.text
