"""Unit tests for node-level capability_scope (FAR-402 P4 / FAR-418).

Covers the four scoping surfaces plus the backcompat guarantee:

* narrow-but-not-widen (compile-time check, widen -> typed error)
* default UNRESTRICTED preserves legacy behaviour (populated from Agent grants,
  not the empty set)
* ConnectorHub fetch-time scoping (deny-by-default: ONLY allowed_connectors are
  decrypted, never post-decrypt filter)
* scope violation -> typed error + metric emission
* allowed_tools narrows via the existing check_tool_scope chokepoint
* context_scope allowlist gates run_context reads
* secret hygiene: connector/secret OBJECTS are rejected as port payloads
* backcompat: pipelines with no capability_scope run unchanged
"""

import json
import uuid
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorResult
from modulo.core.capability_scope import (
    ScopeViolationError,
    agent_granted_connector_types,
    assert_no_secret_objects,
    compute_run_fetch_scope,
    filter_run_context_scope,
    is_connector_allowed,
    validate_allowed_connectors_subset,
)
from modulo.core.connector_hub import ConnectorHub, ConnectorNotFoundError
from modulo.core.mcp.scope_validator import MCPAuthorizationError, check_tool_scope
from modulo.core.pipeline_engine.error_codes import is_retryable, map_legacy_code
from modulo.core.secrets_backend import create_secrets_backend

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_KEY = Fernet.generate_key().decode()


def _encrypt(payload: dict[str, Any]) -> bytes:
    return Fernet(_KEY.encode()).encrypt(json.dumps(payload).encode())


@dataclass
class _FakeCI:
    """Minimal stand-in for ConnectorInstance (no DB needed)."""

    id: uuid.UUID
    connector_type_id: str
    config_json: dict[str, Any] = field(default_factory=dict)
    credentials_ciphertext: bytes = field(default_factory=lambda: _encrypt({}))
    visibility: str = "org"
    allowed_operations: list[str] | None = None


# ---------------------------------------------------------------------------
# Narrow-but-not-widen (compile-time)
# ---------------------------------------------------------------------------


def test_validate_allowed_connectors_subset_accepts_granted_types():
    result = validate_allowed_connectors_subset(
        node_id="n1",
        allowed_connectors=["github", "slack"],
        granted_types={"github", "slack"},
    )
    assert result is None


def test_validate_allowed_connectors_subset_rejects_widen():
    with pytest.raises(ScopeViolationError) as exc_info:
        validate_allowed_connectors_subset(
            node_id="n1",
            allowed_connectors=["github", "not-a-grant"],
            granted_types={"github"},
        )
    assert exc_info.value.node_id == "n1"
    assert exc_info.value.kind == "connector"
    assert exc_info.value.target == "not-a-grant"
    assert "scope.violation" in str(exc_info.value)


def test_validate_allowed_connectors_subset_accepts_instance_ids():
    """Instance-id entries are opaque at compile time and never rejected (run-time enforced)."""
    inst = str(uuid.uuid4())
    result = validate_allowed_connectors_subset(
        node_id="n1",
        allowed_connectors=[inst],
        granted_types=set(),
    )
    assert result is None


def test_validate_allowed_connectors_subset_no_scope_is_unrestricted():
    assert validate_allowed_connectors_subset(node_id="n1", allowed_connectors=None, granted_types=set()) is None
    assert validate_allowed_connectors_subset(node_id="n1", allowed_connectors=[], granted_types=set()) is None


def test_agent_granted_connector_types_handles_both_shapes():
    # dict form
    assert agent_granted_connector_types(
        [{"connector_type": "github", "capabilities": ["issue_read"]}, {"connector_type": "slack"}]
    ) == {"github", "slack"}
    # legacy string-list form
    assert agent_granted_connector_types(["github", "linear"]) == {"github", "linear"}
    # empty / None
    assert not agent_granted_connector_types(None)
    assert not agent_granted_connector_types([])


# ---------------------------------------------------------------------------
# Default UNRESTRICTED (- populated from Agent grants, not empty set)
# ---------------------------------------------------------------------------


def test_effective_default_is_agent_grants_not_empty():
    # UNRESTRICTED (no capability_scope) never yields an empty deny-by-default
    # allow-list: a node may use everything its Agent grants. A node whose grants
    # are narrowed to a non-empty set is validated only as a subset (never widens).
    validate_allowed_connectors_subset(
        node_id="n1",
        allowed_connectors=["github", "slack"],  # agent grants, as a node would declare
        granted_types={"github", "slack"},
    )
    # An absent scope is never a silent deny-by-default for a connector the node
    # actually uses.
    assert is_connector_allowed(
        connector_instance_id=uuid.uuid4(),
        connector_type="github",
        allowed_connectors=None,
    )
    assert is_connector_allowed(
        connector_instance_id=uuid.uuid4(),
        connector_type="github",
        allowed_connectors=[],
    )


def test_is_connector_allowed_unrestricted_default_always_true():
    # A node with no capability_scope may use any connector the hub fetched.
    assert is_connector_allowed(connector_instance_id=uuid.uuid4(), connector_type="github", allowed_connectors=None)
    assert is_connector_allowed(connector_instance_id=uuid.uuid4(), connector_type="github", allowed_connectors=[])


def test_is_connector_allowed_deny_by_default_within_scope():
    inst = uuid.uuid4()
    allowed = is_connector_allowed(connector_instance_id=inst, connector_type="slack", allowed_connectors=["github"])
    assert not allowed
    # By instance-id
    assert is_connector_allowed(connector_instance_id=inst, connector_type="slack", allowed_connectors=[str(inst)])
    # By type
    assert is_connector_allowed(connector_instance_id=inst, connector_type="slack", allowed_connectors=["slack"])


# ---------------------------------------------------------------------------
# ConnectorHub fetch-time scoping (deny-by-default)
# ---------------------------------------------------------------------------


async def test_initialise_fetches_only_allowed_connectors(tmp_path):
    """When allowed_connectors is set, ONLY those connectors are decrypted — the
    secrets backend is never queried for out-of-scope connectors."""
    allowed_id = uuid.uuid4()
    denied_id = uuid.uuid4()
    ci_allowed = _FakeCI(id=allowed_id, connector_type_id="filesystem", config_json={"base_path": str(tmp_path)})
    ci_denied = _FakeCI(id=denied_id, connector_type_id="filesystem", config_json={"base_path": str(tmp_path)})

    fetched: list[str] = []
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")

    async def _get_secret(key: str) -> str:
        fetched.append(key)
        return "{}"

    with patch.object(backend, "get_secret", side_effect=_get_secret):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci_allowed, ci_denied], allowed_connectors=[str(allowed_id)])

    # Only the allowed connector was fetched/decrypted (fetch-time, not post-decrypt).
    assert fetched == [str(allowed_id)]
    assert hub.get(allowed_id) is not None
    with pytest.raises(ConnectorNotFoundError):
        hub.get(denied_id)


async def test_initialise_scope_matches_by_type(tmp_path):
    """allowed_connectors may reference a connector TYPE (not just instance-id)."""
    allowed_id = uuid.uuid4()
    denied_id = uuid.uuid4()
    ci_allowed = _FakeCI(id=allowed_id, connector_type_id="filesystem", config_json={"base_path": str(tmp_path)})
    ci_denied = _FakeCI(id=denied_id, connector_type_id="filesystem", config_json={"base_path": str(tmp_path)})

    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci_allowed, ci_denied], allowed_connectors=["filesystem"])
    assert hub.get(allowed_id) is not None
    assert hub.get(denied_id) is not None  # both are type "filesystem", both in scope


async def test_initialise_unrestricted_default_fetches_all(tmp_path):
    """No allowed_connectors (the default) fetches every instance — legacy behaviour."""
    id1 = uuid.uuid4()
    id2 = uuid.uuid4()
    base = {"base_path": str(tmp_path)}
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise(
            [
                _FakeCI(id=id1, connector_type_id="filesystem", config_json=base),
                _FakeCI(id=id2, connector_type_id="filesystem", config_json=base),
            ]
        )
    assert hub.connector_ids == frozenset({id1, id2})


# ---------------------------------------------------------------------------
# Scope violation -> typed error + metric emission
# ---------------------------------------------------------------------------


def test_scope_violation_is_typed_and_metric_emitting():
    with patch("modulo.core.capability_scope.record_scope_violation") as mock_metric:
        # The message interpolates the structured context.
        with pytest.raises(ScopeViolationError) as exc_info:
            raise ScopeViolationError(node_id="n1", target="slack", kind="connector")
        exc = exc_info.value
        assert exc.node_id == "n1"
        assert exc.target == "slack"
        assert exc.kind == "connector"
        assert "scope.violation node=n1 connector=slack" in str(exc)
        # Metric helper fires without raising (fail-closed).
        mock_metric(node_id="n1", target="slack", kind="connector")
        mock_metric.assert_called_once_with(node_id="n1", target="slack", kind="connector")


def test_scope_violation_is_named_in_error_taxonomy():
    assert map_legacy_code("scope.violation") == "scope.violation"
    assert map_legacy_code("scope_violation") == "scope.violation"
    assert map_legacy_code("ScopeViolationError") == "scope.violation"
    # Permanent (never retryable — re-dispatching reproduces the violation).
    assert is_retryable("scope.violation") is False


# ---------------------------------------------------------------------------
# allowed_tools narrowing via check_tool_scope
# ---------------------------------------------------------------------------


def test_check_tool_scope_narrows_by_allowed_tools():
    # In-scope tool + valid role -> passes.
    check_tool_scope("admin", "create_pipeline", allowed_tools=["create_pipeline"])
    # Out-of-scope tool despite valid role -> rejected.
    with pytest.raises(MCPAuthorizationError):
        check_tool_scope("admin", "create_pipeline", allowed_tools=["create_agent"])


def test_check_tool_scope_no_allowed_tools_means_no_narrowing():
    check_tool_scope("admin", "create_pipeline")
    # Still reject an unknown tool (role policy unchanged).
    with pytest.raises(MCPAuthorizationError):
        check_tool_scope("admin", "sell_land")


def test_check_tool_scope_narrows_via_request_contextvar():
    # FAR-418: the production run path (McpAuthMiddleware -> check_tool_scope)
    # supplies allowed_tools via the request-scoped ContextVar, not the param.
    from modulo.core.mcp.scope_validator import (
        get_request_allowed_tools,
        set_request_allowed_tools,
    )

    set_request_allowed_tools(["create_pipeline", "list_runs"])
    try:
        # In-scope tool + valid role -> passes.
        check_tool_scope("admin", "create_pipeline")
        # Out-of-scope tool despite valid role -> rejected (node-level scoping).
        with pytest.raises(MCPAuthorizationError):
            check_tool_scope("admin", "create_agent")
    finally:
        # Reset the ContextVar to the unrestricted default (None) so the test
        # leaves no residue for other tests in the process.
        set_request_allowed_tools(None)

    # After the context is cleared, no narrowing is applied.
    assert get_request_allowed_tools() is None
    check_tool_scope("admin", "create_agent")


# ---------------------------------------------------------------------------
# context_scope allowlist
# ---------------------------------------------------------------------------


def test_filter_run_context_scope_gates_keys():
    run_context = {"input": {"q": 1}, "org": "a", "secret_bits": 42, "_pipeline_default_autonomy": "auto"}
    scoped = filter_run_context_scope(run_context, ["org"])
    assert scoped == {"input": {"q": 1}, "org": "a"}
    # input is always kept; out-of-list keys dropped; original not mutated.
    assert "secret_bits" in run_context


def test_filter_run_context_scope_unrestricted_returns_unchanged():
    run_context = {"a": 1, "b": 2}
    assert filter_run_context_scope(run_context, None) is run_context
    assert filter_run_context_scope(run_context, []) is run_context


# ---------------------------------------------------------------------------
# Secret hygiene — connector/secret OBJECTS are never port payload types
# ---------------------------------------------------------------------------


async def test_assert_no_secret_objects_rejects_connector():
    connector = await _make_fake_connector()
    with pytest.raises(ScopeViolationError) as exc_info:
        assert_no_secret_objects({"records": [{"ok": True}], "raw": connector}, node_id="n1")
    assert exc_info.value.kind == "secret"


def test_assert_no_secret_objects_rejects_connector_inside_result():
    """The production query path returns a ConnectorResult *dataclass* whose
    ``records`` carry the payload. A connector object smuggled inside
    ``ConnectorResult.records`` must be rejected — the guard must descend into
    dataclass fields, not just dict/list/tuple."""
    from modulo.connectors.base import ConnectorResult

    class _FakeConnector2:
        connector_type = "github"
        query = lambda *a, **k: None  # noqa: E731

    result = ConnectorResult(records=[{"raw": _FakeConnector2(), "data": {"ok": True}}])
    with pytest.raises(ScopeViolationError) as exc_info:
        assert_no_secret_objects(result, node_id="n1")
    assert exc_info.value.kind == "secret"


def test_assert_no_secret_objects_allows_plain_result():
    """A ConnectorResult carrying only plain-serializable records is valid."""
    from modulo.connectors.base import ConnectorResult

    assert_no_secret_objects(ConnectorResult(records=[{"ok": True}]), node_id="n1")


def test_assert_no_secret_objects_allows_plain_data():
    # Opaque connector ID strings + plain dicts/list are valid port payloads.
    assert_no_secret_objects({"connector_id": str(uuid.uuid4()), "records": [{"ok": True}]}, node_id="n1")


# Save-time (route-level) narrow-not-widen control (FAR-418 MINOR-1)
# ---------------------------------------------------------------------------


class _FakeAgent:
    """Minimal stand-in for the db Agent model — only connector_type_refs is read."""

    def __init__(self, connector_type_refs: list[str]) -> None:
        self.connector_type_refs = connector_type_refs


def _scope_node(agent_id: uuid.UUID, allowed_connectors: list[str] | None):
    """Build a PipelineGraphNode with a capability_scope for save-time tests."""
    from modulo.api.routes.pipelines import CapabilityScope, PipelineGraphNode

    payload = {
        "id": str(uuid.uuid4()),
        "node_type": "agent",
        "agent_id": str(agent_id),
        "position": {"x": 0.0, "y": 0.0},
    }
    if allowed_connectors is not None:
        payload["capability_scope"] = CapabilityScope(allowed_connectors=allowed_connectors)
    return PipelineGraphNode.model_validate(payload)


def test_validate_capability_scopes_rejects_widen_at_save_time():
    """The save-time compile-time check is the route's primary security control:
    a node that WIDENS its Agent's connector grants (names a type the Agent was
    not granted) must be rejected. The route converts this typed
    ScopeViolationError into HTTP 422 on pipeline save."""

    from modulo.api.routes.pipelines import _validate_capability_scopes

    agent_id = uuid.uuid4()
    # Agent granted only 'github'.
    node = _scope_node(agent_id, ["github", "linear"])
    with pytest.raises(ScopeViolationError):
        _validate_capability_scopes([node], {agent_id: _FakeAgent(["github"])})


def test_validate_capability_scopes_accepts_narrow_subset():
    """A node that narrows (a strict subset of the Agent's grants) is accepted."""

    from modulo.api.routes.pipelines import _validate_capability_scopes

    agent_id = uuid.uuid4()
    node = _scope_node(agent_id, ["github"])
    # A strict subset of the Agent's grants is accepted: the check returns
    # normally (None) and does not raise.
    result = _validate_capability_scopes([node], {agent_id: _FakeAgent(["github", "linear"])})
    assert result is None


def test_validate_capability_scopes_unrestricted_node_is_skipped():
    """An UNRESTRICTED node (no scope) is not subject to the widen check."""

    from modulo.api.routes.pipelines import _validate_capability_scopes

    agent_id = uuid.uuid4()
    node = _scope_node(agent_id, None)
    # No scope → skipped by the widen check; the call returns normally (None).
    result = _validate_capability_scopes([node], {agent_id: _FakeAgent(["github"])})
    assert result is None


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Backcompat: no capability_scope -> unchanged pipeline behaviour
# ---------------------------------------------------------------------------


async def test_backcompat_pipeline_without_scope_runs_unchanged(tmp_path):
    """A pipeline node with no capability_scope must behave exactly as before:
    the hub fetches all connectors (unrestricted) and any connector is allowed."""
    id1 = uuid.uuid4()
    ci = _FakeCI(id=id1, connector_type_id="filesystem", config_json={"base_path": str(tmp_path)})
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])

    # Node without capability_scope may use the fetched connector.
    assert is_connector_allowed(connector_instance_id=id1, connector_type="filesystem", allowed_connectors=None)
    assert hub.get(id1) is not None


# ---------------------------------------------------------------------------
# Runtime node-level gate (make_connector_fn)
# ---------------------------------------------------------------------------


class _StubConnector:
    """Minimal queryable connector stand-in for the runtime scope gate."""

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        return ConnectorResult(records=[{"ok": True}])


class _StubHub:
    """Minimal hub stand-in: records which instance-ids resolve.

    ``get`` raises when the instance was never resolved, so the deny-by-default
    scope gate (which fires BEFORE ``get``) can be told apart from a missing
    connection.
    """

    def __init__(self, *, resolve: bool = True) -> None:
        self._resolve = resolve
        self.resolved: list[uuid.UUID] = []

    def get(self, instance_id: uuid.UUID) -> Any:
        if not self._resolve:
            raise ConnectorNotFoundError(instance_id)
        self.resolved.append(instance_id)
        return _StubConnector()


async def test_make_connector_fn_scope_exclusion_fails_fast():
    """A connector node whose capability_scope excludes its bound connector fails
    FAST with a typed scope.violation before the hub is ever consulted."""
    from modulo.core.pipeline_engine.decorator import set_connector_hub
    from modulo.core.pipeline_engine.node_runner import make_connector_fn

    inst = uuid.uuid4()
    node_def = {
        "id": str(uuid.uuid4()),
        "connector_binding": {"instance_id": str(inst), "type": "github", "operation": "query"},
        "capability_scope": {"allowed_connectors": ["slack"]},  # excludes github
    }
    hub = _StubHub()
    set_connector_hub(hub)
    try:
        fn = make_connector_fn(node_def, timeout=30)
        result = await fn({"run_context": {"input": {}}})
        artifact = result["artifacts"][0]
        assert artifact["status"] == "failed", artifact
        assert "scope.violation" in artifact["error"], artifact["error"]
        # The hub was never consulted (deny-by-default fires first).
        assert not hub.resolved
    finally:
        set_connector_hub(None)


async def test_make_connector_fn_scope_inclusion_proceeds():
    """A connector node whose capability_scope INCLUDES its bound connector
    proceeds normally — the hub is consulted and the query runs."""
    from modulo.core.pipeline_engine.decorator import set_connector_hub
    from modulo.core.pipeline_engine.node_runner import make_connector_fn

    inst = uuid.uuid4()
    node_def = {
        "id": str(uuid.uuid4()),
        "connector_binding": {"instance_id": str(inst), "type": "github", "operation": "query"},
        "capability_scope": {"allowed_connectors": ["github"]},
    }
    hub = _StubHub()
    set_connector_hub(hub)
    try:
        fn = make_connector_fn(node_def, timeout=30)
        result = await fn({"run_context": {"input": {}}})
        artifact = result["artifacts"][0]
        assert artifact["status"] == "completed", artifact
        assert hub.resolved == [inst]
    finally:
        set_connector_hub(None)


async def test_make_connector_fn_no_scope_uses_any_fetched():
    """No capability_scope (UNRESTRICTED default) — the node may use any
    connector the hub fetched (pre-scope behaviour), so access proceeds."""
    from modulo.core.pipeline_engine.decorator import set_connector_hub
    from modulo.core.pipeline_engine.node_runner import make_connector_fn

    inst = uuid.uuid4()
    node_def = {
        "id": str(uuid.uuid4()),
        "connector_binding": {"instance_id": str(inst), "type": "github", "operation": "query"},
    }
    hub = _StubHub()
    set_connector_hub(hub)
    try:
        fn = make_connector_fn(node_def, timeout=30)
        result = await fn({"run_context": {"input": {}}})
        artifact = result["artifacts"][0]
        assert artifact["status"] == "completed", artifact
        assert hub.resolved == [inst]
    finally:
        set_connector_hub(None)


# ---------------------------------------------------------------------------
# Run-level fetch-time scope (compute_run_fetch_scope)
# ---------------------------------------------------------------------------


def test_fully_scoped_graph_returns_union():
    """When every node is connector-scoped, the run fetch set is the union."""
    gh = str(uuid.uuid4())
    sl = str(uuid.uuid4())
    graph = {
        "nodes": [
            {"capability_scope": {"allowed_connectors": ["github", gh]}},
            {"capability_scope": {"allowed_connectors": ["slack", sl]}},
        ]
    }
    scope = compute_run_fetch_scope(graph)
    assert set(scope) == {"github", "slack", gh, sl}


def test_mixed_graph_does_not_fall_back_to_none():
    """FAR-435 tightening: a run that MIXES scoped and unscoped nodes no longer
    falls back to fetch-everything — it fetches only the scoped union.

    (The old contract returned None/None-fetch-all whenever ANY node was
    unrestricted; that back-compat guarantee is gone. A fully-unrestricted run
    still fetches all — see ``test_fully_unrestricted_run_fetches_all``.)
    """
    gh = str(uuid.uuid4())
    graph = {
        "nodes": [
            {"capability_scope": {"allowed_connectors": ["github", gh]}},
            {"node_type": "transform"},  # no capability_scope → unrestricted
        ]
    }
    scope = compute_run_fetch_scope(graph)
    assert scope is not None
    assert set(scope) == {"github", gh}


def test_scoped_node_with_empty_allowed_connectors_falls_back_to_none():
    """A node scoped on tools/context but unrestricted on connectors → fetch all."""
    graph = {
        "nodes": [
            {
                "capability_scope": {
                    "allowed_connectors": [],
                    "allowed_tools": ["search"],
                }
            },
        ]
    }
    assert compute_run_fetch_scope(graph) is None


def test_empty_graph_returns_none():
    assert compute_run_fetch_scope({"nodes": []}) is None
    assert compute_run_fetch_scope(None) is None


def test_mixed_union_dedupes_across_nodes():
    shared = str(uuid.uuid4())
    graph = {
        "nodes": [
            {"capability_scope": {"allowed_connectors": [shared, "github"]}},
            {"capability_scope": {"allowed_connectors": [shared, "slack"]}},
        ]
    }
    scope = compute_run_fetch_scope(graph)
    assert scope.count(shared) == 1
    assert set(scope) == {shared, "github", "slack"}


# ---------------------------------------------------------------------------
# Executor wiring — prove the run environment passes the computed fetch scope
# through to ConnectorHub.initialise (FAR-418 prove-the-fix).
#
# This is the gap the review flagged: the unit tests cover
# ``compute_run_fetch_scope`` and ``ConnectorHub.initialise`` in isolation, but
# nothing exercised ``_init_run_environment`` -> ``_init_connector_hub`` with a
# scoped graph. Removing the ``allowed_connectors=`` argument at
# executor.py:3120 makes this test fail, so the feature cannot silently regress.
# ---------------------------------------------------------------------------


async def test_run_environment_wires_fetch_scope_to_hub(tmp_path, monkeypatch):
    """When every node is connector-scoped, only the union of allowed
    connectors is decrypted — an excluded connector's secrets entry is never
    fetched. Exercises the real executor wiring end-to-end (with the DB session
    and secrets backend stubbed, the hub confinement logic runs for real)."""
    from unittest.mock import AsyncMock, MagicMock

    from modulo.core.capability_scope import compute_run_fetch_scope
    from modulo.core.connector_hub import ConnectorNotFoundError
    from modulo.core.pipeline_engine.decorator import set_connector_hub
    from modulo.core.pipeline_engine.executor import PipelineExecutor

    # --- Fake ConnectorInstance rows (no DB needed) ---
    class _CI:
        def __init__(self, cid: uuid.UUID, ctype: str) -> None:
            self.id = cid
            self.connector_type_id = ctype
            self.config_json = {"base_path": str(tmp_path)}
            self.credentials_ciphertext = b""
            self.visibility = "org"
            self.allowed_operations = None

    allowed_id = uuid.uuid4()
    denied_id = uuid.uuid4()

    # --- Recording secrets backend: capture every get_secret call ---
    fetched: list[str] = []
    sb = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")

    async def _get_secret(key: str) -> str:
        fetched.append(key)
        return "{}"

    monkeypatch.setattr(sb, "get_secret", _get_secret)
    monkeypatch.setattr("modulo.core.secrets_backend.create_secrets_backend", lambda *a, **k: sb)
    # Declare Redis as genuinely NOT configured so the hub stays on its
    # connector-local bucket (the new FAR-439 fail-closed path raises rather than
    # constructing a shared client). A bare MagicMock makes ``redis_url`` truthy
    # and ``modulo_db.lower()`` a MagicMock, which would send the executor path
    # into ``Redis.from_url(MagicMock)`` and explode.
    monkeypatch.setattr(
        "modulo.settings.get_settings",
        lambda: MagicMock(fernet_key=_KEY, redis_url="", modulo_db="sqlite"),
    )

    # --- Fake async session returning our rows ---
    class _NullCtx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeSession:
        def __init__(self) -> None:
            self._rows = [_CI(allowed_id, "filesystem"), _CI(denied_id, "filesystem")]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def begin(self):
            return _NullCtx()

        async def execute(self, *a, **k):
            res = MagicMock()
            sc = MagicMock()
            sc.all.return_value = self._rows
            res.scalars.return_value = sc
            return res

    # --- Executor with stubbed heavy deps ---
    executor = PipelineExecutor(MagicMock())
    executor._session_factory = lambda: _FakeSession()
    monkeypatch.setattr(executor, "_init_model_backend_hub", AsyncMock(return_value=None))
    monkeypatch.setattr("modulo.core.pipeline_engine.executor.set_rls_org", AsyncMock())
    monkeypatch.setattr("modulo.core.pipeline_engine.executor.set_rls_execution_context", AsyncMock())

    org_id = uuid.uuid4()
    graph = {"nodes": [{"capability_scope": {"allowed_connectors": [str(allowed_id)]}}]}
    # Sanity: the run-level scope really is the single scoped connector.
    assert compute_run_fetch_scope(graph) == [str(allowed_id)]

    set_connector_hub(None)
    try:
        _, connector_hub, _, _ = await executor._init_run_environment(
            org_id=org_id,
            run_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            graph_json=graph,
        )
    finally:
        set_connector_hub(None)

    assert connector_hub is not None
    # Deny-by-default: only the scoped connector was fetched/decrypted; the
    # excluded connector's secrets entry was never read.
    assert fetched == [str(allowed_id)]
    assert connector_hub.get(allowed_id) is not None
    with pytest.raises(ConnectorNotFoundError):
        connector_hub.get(denied_id)
    await connector_hub.__aexit__()


async def test_run_environment_mixed_graph_narrows_to_scoped_union(tmp_path, monkeypatch):
    """FAR-435 tightening (proven end-to-end with a real hub): a run that MIXES a
    connector-scoped node with an unrestricted node no longer fetches every
    connector — it fetches ONLY the scoped node's union. The legacy
    "unrestricted node → fetch everything" back-compat guarantee is gone; the
    unrestricted node loses access to connectors outside the scoped union."""
    from unittest.mock import AsyncMock, MagicMock

    from modulo.core.pipeline_engine.decorator import set_connector_hub
    from modulo.core.pipeline_engine.executor import PipelineExecutor

    class _CI:
        def __init__(self, cid: uuid.UUID) -> None:
            self.id = cid
            self.connector_type_id = "filesystem"
            self.config_json = {"base_path": str(tmp_path)}
            self.credentials_ciphertext = b""
            self.visibility = "org"
            self.allowed_operations = None

    allowed_id = uuid.uuid4()
    denied_id = uuid.uuid4()

    fetched: list[str] = []
    sb = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")

    async def _get_secret(key: str) -> str:
        fetched.append(key)
        return "{}"

    monkeypatch.setattr(sb, "get_secret", _get_secret)
    monkeypatch.setattr("modulo.core.secrets_backend.create_secrets_backend", lambda *a, **k: sb)
    monkeypatch.setattr(
        "modulo.settings.get_settings",
        lambda: MagicMock(fernet_key=_KEY, redis_url="", modulo_db="sqlite"),
    )

    class _NullCtx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeSession:
        def __init__(self) -> None:
            self._rows = [_CI(allowed_id), _CI(denied_id)]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def begin(self):
            return _NullCtx()

        async def execute(self, *a, **k):
            res = MagicMock()
            sc = MagicMock()
            sc.all.return_value = self._rows
            res.scalars.return_value = sc
            return res

    executor = PipelineExecutor(MagicMock())
    executor._session_factory = lambda: _FakeSession()
    monkeypatch.setattr(executor, "_init_model_backend_hub", AsyncMock(return_value=None))
    monkeypatch.setattr("modulo.core.pipeline_engine.executor.set_rls_org", AsyncMock())
    monkeypatch.setattr("modulo.core.pipeline_engine.executor.set_rls_execution_context", AsyncMock())

    # Mixed graph: one scoped node + one unrestricted node.
    graph = {
        "nodes": [
            {"capability_scope": {"allowed_connectors": [str(allowed_id)]}},
            {"node_type": "transform"},  # unrestricted
        ]
    }
    # Sanity: the run-level scope really is the single scoped connector (NOT None).
    assert compute_run_fetch_scope(graph) == [str(allowed_id)]

    set_connector_hub(None)
    try:
        _, connector_hub, _, _ = await executor._init_run_environment(
            org_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            graph_json=graph,
        )
    finally:
        set_connector_hub(None)

    assert connector_hub is not None
    # Deny-by-default: only the scoped connector was fetched/decrypted; the
    # unrestricted node does NOT widen the run to fetch the excluded connector.
    assert fetched == [str(allowed_id)]
    assert connector_hub.get(allowed_id) is not None
    with pytest.raises(ConnectorNotFoundError):
        connector_hub.get(denied_id)
    await connector_hub.__aexit__()


async def test_fully_unrestricted_run_fetches_all(tmp_path, monkeypatch):
    """Legacy guarantee preserved for FULLY-unrestricted runs: when no node
    contributes any connector (no capability_scope, no agent grants), the union
    is empty → ``None`` → the hub fetches every active connector."""
    from unittest.mock import AsyncMock, MagicMock

    from modulo.core.pipeline_engine.decorator import set_connector_hub
    from modulo.core.pipeline_engine.executor import PipelineExecutor

    class _CI:
        def __init__(self, cid: uuid.UUID) -> None:
            self.id = cid
            self.connector_type_id = "filesystem"
            self.config_json = {"base_path": str(tmp_path)}
            self.credentials_ciphertext = b""
            self.visibility = "org"
            self.allowed_operations = None

    id1 = uuid.uuid4()
    id2 = uuid.uuid4()

    fetched: list[str] = []
    sb = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")

    async def _get_secret(key: str) -> str:
        fetched.append(key)
        return "{}"

    monkeypatch.setattr(sb, "get_secret", _get_secret)
    monkeypatch.setattr("modulo.core.secrets_backend.create_secrets_backend", lambda *a, **k: sb)
    monkeypatch.setattr(
        "modulo.settings.get_settings",
        lambda: MagicMock(fernet_key=_KEY, redis_url="", modulo_db="sqlite"),
    )

    class _NullCtx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeSession:
        def __init__(self) -> None:
            self._rows = [_CI(id1), _CI(id2)]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def begin(self):
            return _NullCtx()

        async def execute(self, *a, **k):
            res = MagicMock()
            sc = MagicMock()
            sc.all.return_value = self._rows
            res.scalars.return_value = sc
            return res

    executor = PipelineExecutor(MagicMock())
    executor._session_factory = lambda: _FakeSession()
    monkeypatch.setattr(executor, "_init_model_backend_hub", AsyncMock(return_value=None))
    monkeypatch.setattr("modulo.core.pipeline_engine.executor.set_rls_org", AsyncMock())
    monkeypatch.setattr("modulo.core.pipeline_engine.executor.set_rls_execution_context", AsyncMock())

    # Fully unrestricted run -> run fetch scope is None -> hub fetches all.
    graph = {"nodes": [{"node_type": "transform"}]}
    assert compute_run_fetch_scope(graph) is None

    set_connector_hub(None)
    try:
        _, connector_hub, _, _ = await executor._init_run_environment(
            org_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            graph_json=graph,
        )
    finally:
        set_connector_hub(None)

    assert connector_hub is not None
    assert set(fetched) == {str(id1), str(id2)}
    await connector_hub.__aexit__()


async def test_run_environment_agent_grants_confine_decryption(tmp_path, monkeypatch):
    """Prove-the-fix for the agent-grants fetch path (issue #3): an agent node
    with NO capability_scope derives its run scope from the Agent's
    ``connector_type_refs`` grants, and that grants-derived scope really confines
    decryption in a REAL hub — connectors of a type outside the grants are never
    fetched (``ConnectorNotFoundError``), while a granted type IS fetched.

    This exercises the full grants → scope → hub-confinement chain (the node-scope
    path already has an equivalent real-hub test in
    ``test_run_environment_wires_fetch_scope_to_hub``); without it the agent path
    was only mocked.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from modulo.core.pipeline_engine.decorator import set_connector_hub
    from modulo.core.pipeline_engine.executor import PipelineExecutor

    class _CI:
        def __init__(self, cid: uuid.UUID, ctype: str) -> None:
            self.id = cid
            self.connector_type_id = ctype
            self.config_json = {"base_path": str(tmp_path)}
            self.credentials_ciphertext = b""
            self.visibility = "org"
            self.allowed_operations = None

    agent_id = uuid.uuid4()
    granted_id = uuid.uuid4()  # filesystem connector (within the agent's grants)
    denied_id = uuid.uuid4()  # linear connector (OUTSIDE the agent's grants)

    fetched: list[str] = []
    sb = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")

    async def _get_secret(key: str) -> str:
        fetched.append(key)
        return "{}"

    monkeypatch.setattr(sb, "get_secret", _get_secret)
    monkeypatch.setattr("modulo.core.secrets_backend.create_secrets_backend", lambda *a, **k: sb)
    monkeypatch.setattr(
        "modulo.settings.get_settings",
        lambda: MagicMock(fernet_key=_KEY, redis_url="", modulo_db="sqlite"),
    )

    class _NullCtx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeSession:
        def __init__(self) -> None:
            self._connector_rows = [_CI(granted_id, "filesystem"), _CI(denied_id, "linear")]
            # Agent grants = the connector TYPES this agent may use.
            self._agent_rows = [SimpleNamespace(id=agent_id, connector_type_refs=["filesystem", "github"])]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def begin(self):
            return _NullCtx()

        async def execute(self, stmt, *a, **k):
            entity = stmt.column_descriptions[0]["entity"].__name__
            res = MagicMock()
            sc = MagicMock()
            sc.all.return_value = self._agent_rows if entity == "Agent" else self._connector_rows
            res.scalars.return_value = sc
            return res

    executor = PipelineExecutor(MagicMock())
    executor._session_factory = lambda: _FakeSession()
    monkeypatch.setattr(executor, "_init_model_backend_hub", AsyncMock(return_value=None))
    monkeypatch.setattr("modulo.core.pipeline_engine.executor.set_rls_org", AsyncMock())
    monkeypatch.setattr("modulo.core.pipeline_engine.executor.set_rls_execution_context", AsyncMock())

    graph = {"nodes": [{"id": "agent-node", "node_type": "agent", "agent_id": str(agent_id)}]}
    # Sanity: the agent's grants (filesystem, github) become the run scope.
    assert set(compute_run_fetch_scope(graph, {str(agent_id): {"filesystem", "github"}})) == {
        "filesystem",
        "github",
    }

    set_connector_hub(None)
    try:
        _, connector_hub, _, _ = await executor._init_run_environment(
            org_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            graph_json=graph,
        )
    finally:
        set_connector_hub(None)

    assert connector_hub is not None
    # Only the granted type (filesystem) was decrypted; the out-of-grants connector
    # was never fetched.
    assert fetched == [str(granted_id)]
    assert connector_hub.get(granted_id) is not None
    with pytest.raises(ConnectorNotFoundError):
        connector_hub.get(denied_id)
    await connector_hub.__aexit__()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_fake_connector() -> Any:
    from modulo.connectors.base import ConnectorBase, ConnectorType, HealthResult

    class _FakeConnector(ConnectorBase):
        @property
        def connector_type(self) -> ConnectorType:
            return ConnectorType.CUSTOM

        async def health_check(self) -> HealthResult:
            return HealthResult(ok=True)

        async def query(self, q: ConnectorQuery) -> ConnectorResult:
            return ConnectorResult(records=[])

        async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
            return {}

    return _FakeConnector()
