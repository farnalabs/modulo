"""Route-walk oracle for the break-glass mint-marker (deliverable (B)).

Walks the enumerated secret-bearing route files with ``ast`` and asserts that
every create/update/delete endpoint carries the shared ``deny_break_glass_mint``
DI marker (uniform 403 for break-glass accounts), via a per-site mapping table
(route handler -> has-marker true/false). A route that mints a secret/credential
without the marker fails loudly here, so the enumerated mint surface can never
silently lose its break-glass deny.
"""

import ast
from pathlib import Path

import pytest

from modulo.api.dependencies import deny_break_glass_mint

_ROUTES_DIR = Path(__file__).resolve().parents[3] / "src" / "modulo" / "api" / "routes"

#: Route file -> handler names that MUST carry the ``deny_break_glass_mint`` marker.
#: The secret-bearing mint surface is defined once here and in the route files —
#: this table is the oracle the walker asserts against.
EXPECTED_MINT_MARKED: dict[str, set[str]] = {
    # org API keys — POST "", PUT "/{key_id}", DELETE "/{key_id}"
    "api_keys.py": {
        "create_api_key_endpoint",
        "update_api_key_endpoint",
        "revoke_api_key_endpoint",
    },
    # outbound notification webhooks (NotificationEndpoint) — create/update/delete + restore
    "notifications.py": {
        "create_endpoint",
        "update_endpoint",
        "delete_endpoint",
        "restore_endpoint",
    },
    # trigger webhook HMAC secrets + scheduled (cron/polling/ongoing) triggers
    "triggers.py": {
        "update_cron_config",
        "update_polling_config",
        "update_ongoing_config",
        "create_trigger",
        "update_trigger",
        "delete_trigger",
        "restore_trigger",
        "toggle_trigger",
    },
    # admin trigger-event listing only — no mint surface
    "admin_triggers.py": set(),
    # OAuth/SSO providers (client_id + client_secret)
    "admin_sso.py": {
        "create_provider_endpoint",
        "update_provider_endpoint",
        "delete_provider_endpoint",
        "toggle_provider_endpoint",
    },
    # MCP OAuth clients
    "mcp_oauth.py": {
        "register_oauth_client",
        "remove_oauth_client",
    },
    # MCP setup handoff writes a model-backend API key
    "mcp_setup.py": {
        "complete_model_backend_setup",
    },
    # connector instances (encrypted credentials)
    "connectors.py": {
        "create_connector_endpoint",
        "update_connector_endpoint",
        "delete_connector_endpoint",
    },
    # model backends (encrypted API keys)
    "model_backends.py": {
        "create_model_backend_endpoint",
        "update_model_backend_endpoint",
        "delete_model_backend_endpoint",
    },
    # eval definitions
    "evals.py": {
        "create_eval_definition",
        "update_eval_definition",
        "delete_eval_definition",
        "create_eval_from_run",
    },
    # guardrail config-as-code admin surface (FAR-309 PR B per-scope invariant):
    # the elevated read + propose/apply/reject carry the break-glass deny.
    "guardrail_config.py": {
        "get_guardrail_config_elevated",
        "propose_guardrail_config",
        "apply_guardrail_config",
        "reject_guardrail_config",
    },
    # org guardrail kill-switch (FAR-309 PR B org-global invariant): a
    # break-glass account must never disable (or read) the safety control.
    "admin_orgs.py": {
        "admin_get_org_guardrails_kill_switch",
        "admin_set_org_guardrails_kill_switch",
    },
    # Fernet key rotation — mints a new Fernet key for the whole instance.
    "admin_rotation.py": {
        "rotate_key",
    },
    # product-analytics identity rotation — mints a new HMAC secret.
    "product_analytics_identity.py": {
        "rotate_identity_secret",
    },
}


def _is_router_decorator(dec: ast.expr) -> bool:
    """True when the decorator is a ``<name>.method(...)`` call on a router object."""
    if not isinstance(dec, ast.Call):
        return False
    func = dec.func
    return isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id.endswith("router")


def _mentions_marker(node: ast.expr | None) -> bool:
    """True when the AST subtree references ``deny_break_glass_mint`` by name."""
    if node is None:
        return False
    if isinstance(node, ast.Name):
        return node.id == "deny_break_glass_mint"
    if isinstance(node, ast.Attribute):
        return _mentions_marker(node.value)
    if isinstance(node, ast.Call):
        return any(_mentions_marker(arg) for arg in node.args) or any(
            _mentions_marker(kw.value) for kw in node.keywords
        )
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return any(_mentions_marker(elt) for elt in node.elts)
    if isinstance(node, ast.keyword):
        return _mentions_marker(node.value)
    if isinstance(node, ast.Subscript):
        return _mentions_marker(node.value) or _mentions_marker(node.slice)
    return False


def _route_handlers(tree: ast.Module) -> dict[str, ast.AsyncFunctionDef]:
    """Handler name -> node for every function carrying a router decorator."""
    handlers: dict[str, ast.AsyncFunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and any(_is_router_decorator(dec) for dec in node.decorator_list):
            handlers[node.name] = node
    return handlers


def _has_mint_marker(func_node: ast.AsyncFunctionDef) -> bool:
    """True when the marker appears in a decorator or the function signature."""
    return any(_mentions_marker(dec) for dec in func_node.decorator_list) or any(
        _mentions_marker(default) for default in func_node.args.defaults
    )


def _read_tree(filename: str) -> ast.Module:
    source = (_ROUTES_DIR / filename).read_text(encoding="utf-8")
    return ast.parse(source, filename=filename)


@pytest.mark.parametrize("filename", sorted(EXPECTED_MINT_MARKED))
def test_mint_marker_present_on_all_expected_routes(filename: str) -> None:
    """Every expected handler carries the marker; no unexpected handler does."""
    tree = _read_tree(filename)
    handlers = _route_handlers(tree)
    detected = {name for name, node in handlers.items() if _has_mint_marker(node)}
    expected = EXPECTED_MINT_MARKED[filename]

    missing = expected - detected
    unexpected = detected - expected
    assert not missing, f"{filename}: mint marker missing on {sorted(missing)}"
    assert not unexpected, f"{filename}: unexpected mint marker on {sorted(unexpected)}"


@pytest.mark.parametrize("filename", sorted(EXPECTED_MINT_MARKED))
def test_expected_handlers_exist(filename: str) -> None:
    """The oracle table references handlers that actually exist in the file."""
    tree = _read_tree(filename)
    handlers = _route_handlers(tree)
    missing = EXPECTED_MINT_MARKED[filename] - set(handlers)
    assert not missing, f"{filename}: oracle names not found as route handlers: {sorted(missing)}"


def test_mint_marker_is_importable() -> None:
    assert callable(deny_break_glass_mint)
