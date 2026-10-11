"""FAR-735: REST and MCP gates for eval-definition writes must agree.

The ADR 047 registry is the single floor for ``eval.definition.create`` /
``eval.definition.update`` (operator) and ``eval.definition.delete`` (admin).
This parity test pins the three surfaces together so they cannot drift:

* REST route permission tags (introspected off the live router),
* the MCP tool-scope map (``TOOL_SCOPE_REQUIREMENTS``),
* the registry floors themselves.

The MCP tool-level role banner semantics (operator passes create/update,
delete stays admin-only) are pinned behaviourally in
``tests/unit/mcp/test_mcp_server_coverage.py::TestAssertEvalDefinitionWriteScope``,
and the REST in-handler guard (the second line of defence behind
``require_permission``) is pinned in
``TestRestEvalDefinitionWriteRoleGuard`` below.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from modulo.api.main import app
from modulo.api.routes.evals import _assert_eval_definition_write_role
from modulo.auth.jwt import TenantPrincipal
from modulo.core.mcp.scope_validator import TOOL_SCOPE_REQUIREMENTS
from tests.unit.api.route_introspection import (
    get_all_apiroutes,
    get_permission_tag,
    get_resolved_min_role,
)

# (method, path) -> registry permission the mutating eval-definition writes
# surface must carry. from-run is a create-flavoured write.
_EXPECTED_WRITES: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/evals"): "eval.definition.create",
    ("PUT", "/api/v1/evals/{eval_id}"): "eval.definition.update",
    ("POST", "/api/v1/evals/from-run"): "eval.definition.create",
    ("DELETE", "/api/v1/evals/{eval_id}"): "eval.definition.delete",
}

_MCP_TOOL_TO_PERMISSION = {
    "create_eval_definition": "eval.definition.create",
    "update_eval_definition": "eval.definition.update",
    "delete_eval_definition": "eval.definition.delete",
}


def _collect_route_tags() -> dict[tuple[str, str], dict]:
    tags: dict[tuple[str, str], dict] = {}
    for route in get_all_apiroutes(app):
        tag = get_permission_tag(route)
        if tag is None:
            continue
        for key in _EXPECTED_WRITES:
            if key in tags:
                continue
            if key[0] in (route.methods or set()) and route.path == key[1]:
                tags[key] = tag
    return tags


def test_registry_floors_for_eval_definition_writes() -> None:
    assert get_resolved_min_role("eval.definition.create") == "operator"
    assert get_resolved_min_role("eval.definition.update") == "operator"
    # The registry floor for delete is operator too — but BOTH surfaces
    # enforce a stricter admin gate in the route handler / tool banner
    # (pinned in test_evals_routes_coverage / test_evals_endpoint /
    # test_eval_definition_tools), so delete stays admin-only.
    assert get_resolved_min_role("eval.definition.delete") == "operator"


def test_mcp_tool_scope_map_matches_registry() -> None:
    for tool, permission in _MCP_TOOL_TO_PERMISSION.items():
        assert TOOL_SCOPE_REQUIREMENTS[tool] == permission


def test_rest_write_routes_carry_the_expected_permission_tags() -> None:
    tags = _collect_route_tags()
    missing = set(_EXPECTED_WRITES) - set(tags)
    assert not missing, f"eval-definition write routes missing permission tags: {sorted(missing)}"
    for key, tag in tags.items():
        assert tag["permission"] == _EXPECTED_WRITES[key], key
        assert tag["permission_kind"] == "tenant", key


def test_rest_route_min_roles_match_registry_floors() -> None:
    """REST + MCP cannot disagree: each write route's tag resolves to the same floor."""
    tags = _collect_route_tags()
    for key, tag in tags.items():
        assert get_resolved_min_role(tag["permission"]) == get_resolved_min_role(_EXPECTED_WRITES[key]), key


@pytest.mark.parametrize(
    ("tool", "expected_permission"),
    sorted(_MCP_TOOL_TO_PERMISSION.items()),
    ids=sorted(_MCP_TOOL_TO_PERMISSION),
)
def test_mcp_floors_match_rest_floors(tool: str, expected_permission: str) -> None:
    """The tool-scope permission and the REST route permission share one floor."""
    assert get_resolved_min_role(TOOL_SCOPE_REQUIREMENTS[tool]) == get_resolved_min_role(expected_permission)


def _principal(org_role: str | None) -> TenantPrincipal:
    return TenantPrincipal(
        username="u",
        organisation_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        org_role=org_role,
    )


class TestRestEvalDefinitionWriteRoleGuard:
    """The in-handler REST guard is the second line of defence behind
    ``require_permission``: a caller the registry floor could not stop (a
    legacy role bundle, or a missing role) must still receive 403 rather than
    reaching the write. Exercised directly because the route tests deny
    below-floor callers at the dependency layer, before the handler runs.
    """

    def test_none_role_is_denied(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _assert_eval_definition_write_role(_principal(None), "create")
        assert exc.value.status_code == 403

    def test_below_operator_role_is_denied(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _assert_eval_definition_write_role(_principal("runner"), "update")
        assert exc.value.status_code == 403

    @pytest.mark.parametrize("role", ["operator", "admin"])
    def test_operator_and_admin_are_allowed(self, role: str) -> None:
        _assert_eval_definition_write_role(_principal(role), "create")
