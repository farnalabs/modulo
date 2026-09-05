"""Unit tests for ShellConnector."""

import sys
import types
import uuid
from typing import Any, Self
from unittest.mock import AsyncMock

import pytest

from modulo.connectors.base import (
    ConnectorPayload,
    ConnectorPermissionError,
    ConnectorQuery,
    ConnectorType,
    HealthResult,
)
from modulo.connectors.shell import ShellConnector
from modulo.core.runtime_provider import ProviderNotConfiguredError, WorkspaceSpec

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


class _FakeRuntimeProvider:
    """Controllable test double that captures commands and returns canned output."""

    def __init__(self) -> None:
        self.created_workspaces: list[WorkspaceSpec] = []
        self.executed_commands: list[tuple[str, str]] = []
        self.exec_results: dict[str, dict[str, object]] = {}
        self._file_store: dict[str, str] = {}

    def store_file(self, path: str, content: str) -> None:
        self._file_store[path] = content

    def set_exec_result(self, command_suffix: str, result: dict[str, object]) -> None:
        self.exec_results[command_suffix] = result

    async def create_workspace(self, spec: WorkspaceSpec) -> str:
        self.created_workspaces.append(spec)
        return f"ws-{uuid.uuid4()}"

    async def execute_command(
        self,
        workspace: str,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int = 60,
    ) -> dict[str, object]:
        self.executed_commands.append((workspace, command))

        for suffix, result in self.exec_results.items():
            if command.endswith(suffix):
                return result

        if "cat" in command:
            cat_arg = command.partition("cat ")[2].strip()
            content = self._file_store.get(cat_arg, "")
            if cat_arg in self._file_store:
                return {"exit_code": 0, "stdout": content, "stderr": ""}
            return {
                "exit_code": 1,
                "stdout": "",
                "stderr": f"cat: {cat_arg}: No such file",
            }

        if "ls -1a" in command:
            entries = list(self._file_store.keys())
            names = sorted({e.split("/")[-1] for e in entries})
            lines = "\n".join([".", "..", *names])
            return {"exit_code": 0, "stdout": lines, "stderr": ""}

        if "mkdir" in command and "base64" in command:
            return {"exit_code": 0, "stdout": "", "stderr": ""}

        if "echo" in command:
            return {"exit_code": 0, "stdout": "hello", "stderr": ""}

        return {"exit_code": 0, "stdout": "", "stderr": ""}

    async def destroy_workspace(self, provider_ref: str) -> None:
        pass

    async def get_workspace_status(self, provider_ref: str) -> str:
        return "running"


@pytest.fixture
def provider() -> _FakeRuntimeProvider:
    return _FakeRuntimeProvider()


@pytest.fixture
def provider_ref() -> str:
    return "ws-test-001"


@pytest.fixture
def workspace_lease_id() -> uuid.UUID:
    return uuid.uuid4()


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def _connector(
    provider: _FakeRuntimeProvider | None = None,
    lease_id: uuid.UUID | None = None,
    allowed: list[str] | None = None,
) -> ShellConnector:
    return ShellConnector(
        runtime_provider=provider,
        workspace_lease_id=lease_id or uuid.uuid4(),
        allowed_commands=allowed,
    )


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_connector_type(provider: _FakeRuntimeProvider, workspace_lease_id: uuid.UUID) -> None:
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id)
    assert c.connector_type == ConnectorType.SHELL


async def test_health_ok(provider: _FakeRuntimeProvider, workspace_lease_id: uuid.UUID) -> None:
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id)
    result = await c.health_check()
    assert result == HealthResult(ok=True, detail="ShellConnector ready")


async def test_health_fail_no_provider(workspace_lease_id: uuid.UUID) -> None:
    c = ShellConnector(runtime_provider=None, workspace_lease_id=workspace_lease_id)  # type: ignore[arg-type]
    result = await c.health_check()
    assert result.ok is False
    assert "not configured" in result.detail


# ---------------------------------------------------------------------------
# Query — read_file
# ---------------------------------------------------------------------------


async def test_read_file(provider: _FakeRuntimeProvider, provider_ref: str, workspace_lease_id: uuid.UUID) -> None:
    provider.store_file("/tmp/hello.txt", "world")
    c = _connector(provider, workspace_lease_id)

    result = await c.query(
        ConnectorQuery(
            resource="file",
            filters={"path": "/tmp/hello.txt", "provider_ref": provider_ref},
        )
    )
    assert len(result.records) == 1
    assert result.records[0]["content"] == "world"
    assert result.records[0]["path"] == "/tmp/hello.txt"


async def test_read_file_missing(
    provider: _FakeRuntimeProvider, provider_ref: str, workspace_lease_id: uuid.UUID
) -> None:
    c = _connector(provider, workspace_lease_id)

    with pytest.raises(ValueError, match="Failed to read file"):
        await c.query(
            ConnectorQuery(
                resource="file",
                filters={"path": "/nonexistent", "provider_ref": provider_ref},
            )
        )


async def test_read_file_without_provider_ref(provider: _FakeRuntimeProvider, workspace_lease_id: uuid.UUID) -> None:
    """Reading a file does not require a provider_ref — it is optional for direct providers."""
    provider.store_file("/tmp/x.txt", "world")
    c = _connector(provider, workspace_lease_id)

    result = await c.query(ConnectorQuery(resource="file", filters={"path": "/tmp/x.txt"}))
    assert result.records[0]["content"] == "world"


# ---------------------------------------------------------------------------
# Query — list_directory
# ---------------------------------------------------------------------------


async def test_list_directory(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    provider.store_file("/workspace/a.txt", "a")
    provider.store_file("/workspace/b.txt", "b")
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id)

    result = await c.query(
        ConnectorQuery(
            resource="directory",
            filters={"path": "/workspace", "provider_ref": provider_ref},
        )
    )
    names = [r["name"] for r in result.records]
    assert "a.txt" in names
    assert "b.txt" in names
    assert result.total == 2


async def test_list_directory_default_path(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    provider.store_file("/tmp/test.txt", "data")
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id)

    result = await c.query(
        ConnectorQuery(
            resource="directory",
            filters={"provider_ref": provider_ref},
        )
    )
    assert result.total == 1


# ---------------------------------------------------------------------------
# Query — unsupported resource
# ---------------------------------------------------------------------------


async def test_unsupported_query_resource(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id)

    with pytest.raises(ValueError, match="Unsupported shell query resource"):
        await c.query(
            ConnectorQuery(
                resource="blob",
                filters={"provider_ref": provider_ref},
            )
        )


# ---------------------------------------------------------------------------
# Write — run_command
# ---------------------------------------------------------------------------


async def test_run_command(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    provider.set_exec_result(
        "echo hello",
        {"exit_code": 0, "stdout": "hello\n", "stderr": "", "duration_ms": 15},
    )
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id, allowed_commands=["echo"])

    result = await c.write(
        ConnectorPayload(
            resource="command",
            data={
                "command": "echo hello",
                "provider_ref": provider_ref,
            },
        )
    )
    assert result["stdout"] == "hello\n"
    assert result["exit_code"] == 0
    assert result["duration_ms"] == 15
    assert result["masked"] is True

    # Verify the command was captured
    assert len(provider.executed_commands) == 1
    recorded_ref, recorded_cmd = provider.executed_commands[0]
    assert recorded_ref == provider_ref
    assert "echo" in recorded_cmd


async def test_run_command_with_cwd(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id, allowed_commands=["make"])

    await c.write(
        ConnectorPayload(
            resource="command",
            data={
                "command": "make build",
                "cwd": "/workspace/project",
                "provider_ref": provider_ref,
            },
        )
    )

    assert len(provider.executed_commands) == 1
    _, cmd = provider.executed_commands[0]
    assert "cd" in cmd
    assert "/workspace/project" in cmd
    assert "make build" in cmd


async def test_run_command_with_env(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id, allowed_commands=["npm"])

    await c.write(
        ConnectorPayload(
            resource="command",
            data={
                "command": "npm test",
                "env": {"NODE_ENV": "test", "CI": "true"},
                "provider_ref": provider_ref,
            },
        )
    )

    assert len(provider.executed_commands) == 1
    _, cmd = provider.executed_commands[0]
    assert "NODE_ENV=test" in cmd
    assert "CI=true" in cmd
    assert "npm test" in cmd


async def test_run_command_with_timeout(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id, allowed_commands=["sleep"])

    await c.write(
        ConnectorPayload(
            resource="command",
            data={
                "command": "sleep 10",
                "timeout_seconds": 5,
                "provider_ref": provider_ref,
            },
        )
    )

    assert len(provider.executed_commands) == 1
    _, cmd = provider.executed_commands[0]
    assert "sleep" in cmd


# ---------------------------------------------------------------------------
# Write — command allowlist enforcement
# ---------------------------------------------------------------------------


async def test_run_command_deny_all_default(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id)

    with pytest.raises(ConnectorPermissionError, match="deny-all"):
        await c.write(
            ConnectorPayload(
                resource="command",
                data={
                    "command": "echo hello",
                    "provider_ref": provider_ref,
                },
            )
        )


async def test_run_command_not_in_allowlist(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    c = ShellConnector(
        runtime_provider=provider, workspace_lease_id=workspace_lease_id, allowed_commands=["echo", "cat"]
    )

    with pytest.raises(ConnectorPermissionError, match="not in the allowed list"):
        await c.write(
            ConnectorPayload(
                resource="command",
                data={
                    "command": "rm -rf /",
                    "provider_ref": provider_ref,
                },
            )
        )


async def test_run_command_allowed(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    c = ShellConnector(
        runtime_provider=provider, workspace_lease_id=workspace_lease_id, allowed_commands=["echo", "cat", "ls"]
    )

    result = await c.write(
        ConnectorPayload(
            resource="command",
            data={
                "command": "echo allowed",
                "provider_ref": provider_ref,
            },
        )
    )
    assert result["exit_code"] == 0


# ---------------------------------------------------------------------------
# Write — write_file
# ---------------------------------------------------------------------------


async def test_write_file(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id)

    result = await c.write(
        ConnectorPayload(
            resource="file",
            data={
                "path": "/workspace/out.txt",
                "content": "hello world",
                "provider_ref": provider_ref,
            },
        )
    )
    assert result["path"] == "/workspace/out.txt"
    assert result["bytes_written"] == 11
    assert result["exit_code"] == 0


async def test_write_file_without_provider_ref(
    provider: _FakeRuntimeProvider,
    workspace_lease_id: uuid.UUID,
) -> None:
    """Writing a file does not require a provider_ref — it is optional for direct providers."""
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id)

    result = await c.write(
        ConnectorPayload(
            resource="file",
            data={"path": "/tmp/x.txt", "content": "data"},
        )
    )
    assert result["path"] == "/tmp/x.txt"
    assert result["bytes_written"] == 4
    assert result["exit_code"] == 0


# ---------------------------------------------------------------------------
# Write — unsupported resource
# ---------------------------------------------------------------------------


async def test_unsupported_write_resource(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id)

    with pytest.raises(ValueError, match="Unsupported shell write resource"):
        await c.write(
            ConnectorPayload(
                resource="binary",
                data={"provider_ref": provider_ref},
            )
        )


# ---------------------------------------------------------------------------
# Edge cases — capabilities, empty inputs, combined options
# ---------------------------------------------------------------------------


def test_connector_type_capabilities() -> None:
    assert ConnectorType.SHELL.capabilities == frozenset({"read", "write"})


async def test_run_command_empty_allowlist(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    """Explicitly passing allowed_commands=[] should behave as deny-all."""
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id, allowed_commands=[])

    with pytest.raises(ConnectorPermissionError, match="deny-all"):
        await c.write(
            ConnectorPayload(
                resource="command",
                data={"command": "echo x", "provider_ref": provider_ref},
            )
        )


async def test_run_command_empty_command(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    """An empty command string should raise permission error (base cmd is '')."""
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id, allowed_commands=["echo"])

    with pytest.raises(ConnectorPermissionError, match="not in the allowed list"):
        await c.write(
            ConnectorPayload(
                resource="command",
                data={"command": "", "provider_ref": provider_ref},
            )
        )


async def test_run_command_without_provider_ref(
    provider: _FakeRuntimeProvider,
    workspace_lease_id: uuid.UUID,
) -> None:
    """Running a command does not require a provider_ref — it is optional for direct providers."""
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id, allowed_commands=["echo"])

    result = await c.write(
        ConnectorPayload(
            resource="command",
            data={"command": "echo x"},
        )
    )
    assert result["exit_code"] == 0
    assert result["stdout"] == "hello"
    assert result["masked"] is True


async def test_run_command_with_cwd_and_env(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    """Both cwd and env should be combined in the exec command."""
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id, allowed_commands=["npm"])

    await c.write(
        ConnectorPayload(
            resource="command",
            data={
                "command": "npm run build",
                "cwd": "/workspace/project",
                "env": {"NODE_ENV": "production"},
                "provider_ref": provider_ref,
            },
        )
    )

    assert len(provider.executed_commands) == 1
    _, cmd = provider.executed_commands[0]
    assert "cd" in cmd
    assert "/workspace/project" in cmd
    assert "NODE_ENV=production" in cmd
    assert "npm run build" in cmd


async def test_write_file_failure(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    """Write file when runtime provider returns non-zero exit code."""
    provider.set_exec_result(
        "base64 -d > /tmp/readonly.txt",
        {"exit_code": 1, "stdout": "", "stderr": "mkdir: Permission denied"},
    )
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id)

    with pytest.raises(ValueError, match="Failed to write file"):
        await c.write(
            ConnectorPayload(
                resource="file",
                data={
                    "path": "/tmp/readonly.txt",
                    "content": "data",
                    "provider_ref": provider_ref,
                },
            )
        )


async def test_empty_directory_listing(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    """Listing an empty directory should return no records."""
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id)

    result = await c.query(
        ConnectorQuery(
            resource="directory",
            filters={"path": "/empty", "provider_ref": provider_ref},
        )
    )
    assert not result.records
    assert result.total == 0


async def test_write_file_special_chars(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    """Write file content with special characters survives base64 round-trip."""
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id)

    result = await c.write(
        ConnectorPayload(
            resource="file",
            data={
                "path": "/workspace/special.txt",
                "content": "héllo wörld\nline2\n$PATH\n`backtick`",
                "provider_ref": provider_ref,
            },
        )
    )
    assert result["path"] == "/workspace/special.txt"
    assert result["exit_code"] == 0


async def test_run_command_stderr_output(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    """Command stderr is captured and returned."""
    provider.set_exec_result(
        "grep foo",
        {"exit_code": 1, "stdout": "", "stderr": "grep: No such file", "duration_ms": 5},
    )
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id, allowed_commands=["grep"])

    result = await c.write(
        ConnectorPayload(
            resource="command",
            data={
                "command": "grep foo",
                "provider_ref": provider_ref,
            },
        )
    )
    assert result["stderr"] == "grep: No such file"
    assert result["exit_code"] == 1
    assert result["masked"] is True


# ---------------------------------------------------------------------------
# _resolve_profile_from_hub — tenant scoping (FAR-587)
# ---------------------------------------------------------------------------

_ORG_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ORG_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
_PROFILE_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


class _FakeTransaction:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def __aenter__(self) -> Self:
        self._events.append("begin")
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _FakeSession:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _FakeTransaction:
        return _FakeTransaction(self._events)


class _FakeSessionFactory:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def __call__(self) -> _FakeSession:
        return _FakeSession(self._events)


class _RlsAwareDb:
    """Fake DB layer whose profile fetch honours the org passed to set_rls_org.

    Mirrors the production scoping contract: ``set_rls_org`` pins the tenant
    context and the fetch resolves only rows owned by that org (Postgres RLS
    on the real backend, the ``do_orm_execute`` tenant filter on generic
    backends). The CRUD helper is the fetch entrypoint, so a regression to a
    raw unscoped ``select`` never reaches this fake and fails the assertions.
    """

    def __init__(self, profile_org: uuid.UUID) -> None:
        self.events: list[str] = []
        self.rls_org: uuid.UUID | None = None
        self._profile_org = profile_org

    async def fake_set_rls_org(self, session: _FakeSession, org_id: uuid.UUID | None) -> None:
        self.events.append("set_rls_org")
        self.rls_org = org_id

    async def fake_get_environment_profile(self, session: _FakeSession, profile_id: uuid.UUID) -> Any:
        self.events.append("fetch")
        if self.rls_org is not None and self.rls_org != self._profile_org:
            return None
        return {"id": str(profile_id)}


def _patch_db(monkeypatch: pytest.MonkeyPatch, db: _RlsAwareDb) -> None:
    # ``modulo.db.session`` builds its engine from settings at import time, so
    # it cannot be imported in a bare unit-test environment; inject a fake
    # module for it (the function under test imports ``AsyncSessionLocal``
    # lazily at call time, so sys.modules wins). The other two imports resolve
    # against the real modules (both import cleanly without settings).
    fake_session_mod = types.ModuleType("modulo.db.session")
    fake_session_mod.AsyncSessionLocal = _FakeSessionFactory(db.events)
    monkeypatch.setitem(sys.modules, "modulo.db.session", fake_session_mod)
    monkeypatch.setattr("modulo.db.rls.set_rls_org", db.fake_set_rls_org)
    monkeypatch.setattr(
        "modulo.db.crud.environment_profile.get_environment_profile",
        db.fake_get_environment_profile,
    )


def _hub_connector(org_id: str | None) -> ShellConnector:
    return ShellConnector(
        runtime_provider=None,
        runtime_provider_hub=object(),
        environment_profile_id=_PROFILE_ID,
        org_id=org_id,
    )


async def test_resolve_profile_sets_rls_org_before_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The connector's own org pins the RLS context inside the transaction, BEFORE the profile SELECT."""
    db = _RlsAwareDb(profile_org=_ORG_A)
    _patch_db(monkeypatch, db)
    c = _hub_connector(str(_ORG_A))

    profile = await c._resolve_profile_from_hub()

    assert profile is not None
    assert db.events == ["begin", "set_rls_org", "fetch"]
    assert db.rls_org == _ORG_A


async def test_resolve_profile_never_crosses_org_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """An org-B-owned profile row is invisible to an org-A connector; the owning org still resolves it."""
    db = _RlsAwareDb(profile_org=_ORG_B)
    _patch_db(monkeypatch, db)
    org_a_connector = _hub_connector(str(_ORG_A))

    foreign_profile = await org_a_connector._resolve_profile_from_hub()

    assert foreign_profile is None
    assert db.rls_org == _ORG_A

    org_b_connector = _hub_connector(str(_ORG_B))
    owned_profile = await org_b_connector._resolve_profile_from_hub()

    assert owned_profile is not None
    assert db.rls_org == _ORG_B


async def test_resolve_profile_without_org_runs_unscoped_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no org claim, set_rls_org receives None (documented no-op) and the CRUD fetch still resolves."""
    db = _RlsAwareDb(profile_org=_ORG_B)
    _patch_db(monkeypatch, db)
    c = _hub_connector(None)

    profile = await c._resolve_profile_from_hub()

    assert profile is not None
    assert db.events == ["begin", "set_rls_org", "fetch"]
    assert db.rls_org is None


async def test_resolve_profile_without_profile_id_skips_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """No environment_profile_id resolves to None without opening a session."""
    db = _RlsAwareDb(profile_org=_ORG_A)
    _patch_db(monkeypatch, db)
    c = ShellConnector(
        runtime_provider=None,
        runtime_provider_hub=object(),
        environment_profile_id=None,
        org_id=str(_ORG_A),
    )

    profile = await c._resolve_profile_from_hub()

    assert profile is None
    assert not db.events


# ---------------------------------------------------------------------------
# Provider-not-configured fail-soft contract (hub-driven resolution)
# ---------------------------------------------------------------------------


class _StubHub:
    """A hub whose resolve() always reports the requested provider as unconfigured."""

    def resolve(self, profile: Any) -> Any:
        raise ProviderNotConfiguredError(getattr(profile, "provider_type", "unknown"))


def test_constructor_invalid_org_id_is_none() -> None:
    """A non-UUID org_id is tolerated and stored as None (S1192/ValueError branch)."""
    c = ShellConnector(runtime_provider=None, org_id="not-a-uuid")

    assert c._org_id is None


async def test_query_unconfigured_provider_from_hub_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hub's ProviderNotConfiguredError is surfaced as a plain ValueError."""
    c = ShellConnector(
        runtime_provider=None,
        runtime_provider_hub=_StubHub(),
        environment_profile_id=uuid.uuid4(),
    )
    monkeypatch.setattr(c, "_resolve_profile_from_hub", AsyncMock(return_value=_profile()))

    with pytest.raises(ValueError, match="Runtime provider not configured"):
        await c.query(ConnectorQuery(resource="file", filters={"path": "/tmp/x.txt"}))


async def test_write_unconfigured_provider_from_hub_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hub's ProviderNotConfiguredError is surfaced as a plain ValueError."""
    c = ShellConnector(
        runtime_provider=None,
        runtime_provider_hub=_StubHub(),
        environment_profile_id=uuid.uuid4(),
    )
    monkeypatch.setattr(c, "_resolve_profile_from_hub", AsyncMock(return_value=_profile()))

    with pytest.raises(ValueError, match="Runtime provider not configured"):
        await c.write(ConnectorPayload(resource="command", data={"command": "echo hi"}))


def _profile() -> Any:
    return type("P", (), {"provider_type": "runner_docker"})()
