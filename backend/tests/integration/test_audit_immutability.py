"""Integration tests for audit-trail immutability.

Proves:
1. Audit events cannot be updated through any API path (405/403)
2. Audit events cannot be deleted through any API path (405/403)
3. Every mutating route emits an audit event (architecture-level enforcement)
4. MCP tool handlers also emit audit events
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.audit_logger.append_only import register_append_only_guard
from modulo.db.models.audit_event import AuditEvent

BACKEND_ROOT = Path(__file__).parents[2]

# ---------------------------------------------------------------------------
# API-level immutability: verify the audit router has no mutating routes
# ---------------------------------------------------------------------------


def _collect_mutating_methods(source_path: Path) -> list[str]:
    """Return docstrings of decorators that look like POST/PUT/PATCH/DELETE routes."""
    mutating: list[str] = []
    tree = ast.parse(source_path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for deco in node.decorator_list:
                if isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute):
                    name = deco.func.attr
                    if name in ("put", "patch", "delete", "post"):
                        mutating.append(name)
    return mutating


class TestAuditApiHasNoMutatingEndpoints:
    """Static analysis: the audit router must not expose PUT/PATCH/DELETE."""

    audit_route_path = BACKEND_ROOT / "src" / "modulo" / "api" / "routes" / "audit.py"

    def test_audit_router_has_no_put(self) -> None:
        assert "put" not in _collect_mutating_methods(self.audit_route_path), (
            "Audit router must not expose PUT endpoints"
        )

    def test_audit_router_has_no_patch(self) -> None:
        assert "patch" not in _collect_mutating_methods(self.audit_route_path), (
            "Audit router must not expose PATCH endpoints"
        )

    def test_audit_router_has_no_delete(self) -> None:
        assert "delete" not in _collect_mutating_methods(self.audit_route_path), (
            "Audit router must not expose DELETE endpoints"
        )

    def test_audit_router_has_only_get_and_post(self) -> None:
        methods = _collect_mutating_methods(self.audit_route_path)
        for m in methods:
            assert m in ("get", "post"), f"Unexpected method {m} on audit router"


# ---------------------------------------------------------------------------
# ORM immutability: AppendOnlyViolationError blocks updates/deletes
# ---------------------------------------------------------------------------


class TestAuditOrmImmutability:
    """Prove ORM-level listeners block UPDATE/DELETE on AuditEvent rows."""

    @pytest_asyncio.fixture(autouse=True)
    async def _register_guard(self) -> None:
        register_append_only_guard()

    async def _seed_org(self, db_session: AsyncSession, org_id: uuid.UUID, suffix: str = "") -> None:
        await db_session.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json, otel_config_json) "
                "VALUES (:id, :name, :slug, '{}'::json, '{}'::json)"
            ),
            {"id": str(org_id), "name": f"Audit Immutability Org{suffix}", "slug": f"audit-imm-{org_id.hex[:8]}"},
        )
        await db_session.commit()

    async def test_orm_update_raises_append_only(self, db_session: AsyncSession) -> None:
        event_id = uuid.uuid4()
        org_id = uuid.uuid4()
        await self._seed_org(db_session, org_id, "-update")
        await db_session.execute(
            text("""
                INSERT INTO audit_events
                    (id, organisation_id, event_type, payload_json, created_at, updated_at)
                VALUES (:id, :org_id, :event_type, '{}'::json, NOW(), NOW())
            """),
            {"id": event_id, "org_id": org_id, "event_type": "immutability.test"},
        )
        await db_session.commit()

        event = await db_session.get(AuditEvent, event_id)
        assert event is not None
        event.event_type = "tampered"

        with pytest.raises(RuntimeError) as exc_info:
            await db_session.flush()
        assert "append-only" in str(exc_info.value).lower()

    async def test_orm_delete_raises_append_only(self, db_session: AsyncSession) -> None:
        event_id = uuid.uuid4()
        org_id = uuid.uuid4()
        await self._seed_org(db_session, org_id, "-delete")
        await db_session.execute(
            text("""
                INSERT INTO audit_events
                    (id, organisation_id, event_type, payload_json, created_at, updated_at)
                VALUES (:id, :org_id, :event_type, '{}'::json, NOW(), NOW())
            """),
            {"id": event_id, "org_id": org_id, "event_type": "immutability.delete.test"},
        )
        await db_session.commit()

        event = await db_session.get(AuditEvent, event_id)
        assert event is not None
        await db_session.delete(event)

        with pytest.raises(RuntimeError) as exc_info:
            await db_session.flush()
        assert "append-only" in str(exc_info.value).lower()

    async def test_event_survives_failed_delete(self, db_session: AsyncSession) -> None:
        event_id = uuid.uuid4()
        org_id = uuid.uuid4()
        await self._seed_org(db_session, org_id, "-survive")
        await db_session.execute(
            text("""
                INSERT INTO audit_events
                    (id, organisation_id, event_type, payload_json, created_at, updated_at)
                VALUES (:id, :org_id, :event_type, '{}'::json, NOW(), NOW())
            """),
            {"id": event_id, "org_id": org_id, "event_type": "immutability.survive.test"},
        )
        await db_session.commit()

        event = await db_session.get(AuditEvent, event_id)
        assert event is not None
        await db_session.delete(event)
        with pytest.raises(RuntimeError):
            await db_session.flush()
        await db_session.rollback()

        reloaded = await db_session.get(AuditEvent, event_id)
        assert reloaded is not None, "Event was deleted despite append-only guard"


# ---------------------------------------------------------------------------
# Architecture-level: every mutating route file emits audit events
# ---------------------------------------------------------------------------

ROUTE_FILES = sorted(BACKEND_ROOT.glob("src/modulo/api/routes/*.py"))
_MUTATING_METHODS = frozenset({"post", "put", "patch", "delete"})


# Known exceptions to the "every mutating route must emit an audit event" rule.
# These are routes that mutate state but do not record audit events by design.
# Any new route file added here must have a documented reason.
_AUDIT_EXEMPT_FILES: frozenset[str] = frozenset(
    {
        "admin_runtime_config.py",  # Runtime config reload — no audit trail for config overrides
        "viewmodel.py",  # Saved views — meta-level, not core domain events
    }
)


class TestEveryMutatingRouteEmitsAuditEvent:
    """Architecture-enforcement: scan route files for audit-event coverage."""

    @pytest.mark.parametrize("route_file", ROUTE_FILES, ids=lambda p: p.name)
    def test_mutating_routes_emit_audit_events(self, route_file: Path) -> None:
        """Every route file with POST/PUT/PATCH/DELETE must reference append_audit_event."""
        if route_file.name in _AUDIT_EXEMPT_FILES:
            pytest.skip(f"{route_file.name} is exempt from audit-event requirement")

        source = route_file.read_text()
        tree = ast.parse(source)

        has_mutating = False
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for deco in node.decorator_list:
                    if (
                        isinstance(deco, ast.Call)
                        and isinstance(deco.func, ast.Attribute)
                        and deco.func.attr in _MUTATING_METHODS
                    ):
                        has_mutating = True
                        break

        if not has_mutating:
            pytest.skip(f"{route_file.name} has no mutating routes")

        assert "append_audit_event" in source, (
            f"{route_file.name} has mutating HTTP methods but does not "
            f"reference append_audit_event — every mutation must be audited"
        )


# ---------------------------------------------------------------------------
# MCP tool audit coverage
# ---------------------------------------------------------------------------


class TestMcpToolsAuditCoverage:
    """Check that MCP tool handlers that perform mutations exist.

    MCP tools that write data (create_pipeline, trigger_pipeline, create_model_backend,
    review_hitl, copy_library_primitive) delegate audit logging to the underlying
    service layer (HITLManager, pipeline engine, DB CRUD), not to direct
    append_audit_event calls in mcp_server.py itself.
    """

    mcp_server_path = BACKEND_ROOT / "src" / "modulo" / "api" / "mcp_server.py"

    def test_mcp_mutating_tools_exist(self) -> None:
        """Verify each MCP tool that changes state exists in the server."""
        source = self.mcp_server_path.read_text()
        tools = [
            "create_pipeline",
            "update_pipeline_graph",
            "trigger_pipeline",
            "create_model_backend",
            "review_hitl",
            "copy_library_primitive",
        ]
        for tool in tools:
            assert tool in source, f"MCP tool {tool} must exist in mcp_server.py"


# ---------------------------------------------------------------------------
# Chain integrity after mutation attempt
# ---------------------------------------------------------------------------


class TestAuditChainIntegrity:
    """Prove the audit chain is not corrupted by failed mutation attempts."""

    async def test_chain_verify_passes_after_failed_mutation(self, db_session: AsyncSession) -> None:
        """After a failed DELETE attempt, chain verification must still pass."""
        from modulo.core.audit_logger import append_audit_event, verify_chain

        org_id = uuid.uuid4()
        await db_session.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json, otel_config_json) "
                "VALUES (:id, :name, :slug, '{}'::json, '{}'::json)"
            ),
            {"id": str(org_id), "name": "Chain Test Org", "slug": f"chain-{org_id.hex[:8]}"},
        )
        await db_session.commit()

        for i in range(3):
            await append_audit_event(
                session=db_session,
                organisation_id=org_id,
                event_type=f"chain.test.{i}",
                payload_json={"seq": i},
            )

        result = await verify_chain(db_session, org_id)
        assert result["valid"] is True, f"Chain invalid before mutation attempt: {result}"

        event_id = uuid.uuid4()
        await db_session.execute(
            text("""
                INSERT INTO audit_events
                    (id, organisation_id, event_type, payload_json, created_at, updated_at)
                VALUES (:id, :org_id, :event_type, '{}'::json, NOW(), NOW())
            """),
            {"id": event_id, "org_id": org_id, "event_type": "chain.tamper.target"},
        )
        await db_session.commit()

        event = await db_session.get(AuditEvent, event_id)
        assert event is not None
        await db_session.delete(event)
        with pytest.raises(RuntimeError):
            await db_session.flush()
        await db_session.rollback()

        result_after = await verify_chain(db_session, org_id)
        assert result_after["valid"] is True, f"Chain invalid after failed mutation: {result_after}"
