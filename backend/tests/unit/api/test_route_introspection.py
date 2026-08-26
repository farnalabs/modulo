"""Strict route-introspection test for the ADR 017 permission sweep.

Walks the FastAPI router and asserts that every mutating user-principal route
carries a permission dependency (tagged via ``_tagged_dep``) OR is on the
explicit documented exempt allowlist. This is the completeness guarantee for
task-authz-b-sweep: a route added without a permission tag fails loudly.

The ADR's intent (v9, "Phase-1 sweep" + "Strict introspection test"):
- bidirectional map: every mutating route is tagged or exempt; nothing is both
- variant-kind assertions: strict system routes use require_system_permission,
  org-deletion uses require_system_or_org_admin, admin_email/orgs use the
  scoped-hybrid, run trigger/status use tenant_or_api_key
- min-role assertions: the resolved role matches the registry
"""

from __future__ import annotations

from collections import Counter

import pytest

from modulo.api.main import app
from tests.unit.api.route_introspection import (
    get_all_apiroutes,
    get_mutating_routes,
    get_permission_tag,
    get_resolved_min_role,
)

# ---------------------------------------------------------------------------
# Exempt allowlist: mutating routes that intentionally do NOT carry a
# permission tag, each with a documented reason (ADR 017 exempt channels).
# Key = (method, path) ; value = reason.
# ---------------------------------------------------------------------------
EXEMPT: dict[tuple[str, str], str] = {
    # SCIM: shared-secret MODULO_SCIM_TOKEN is the authorization (phase 1).
    ("POST", "/scim/v2/Users"): "SCIM shared-secret channel",
    ("PUT", "/scim/v2/Users/{user_id}"): "SCIM shared-secret channel",
    ("PATCH", "/scim/v2/Users/{user_id}"): "SCIM shared-secret channel",
    ("DELETE", "/scim/v2/Users/{user_id}"): "SCIM shared-secret channel",
    ("POST", "/scim/v2/Groups"): "SCIM shared-secret channel",
    ("PUT", "/scim/v2/Groups/{group_id}"): "SCIM shared-secret channel",
    ("PATCH", "/scim/v2/Groups/{group_id}"): "SCIM shared-secret channel",
    ("DELETE", "/scim/v2/Groups/{group_id}"): "SCIM shared-secret channel",
    # Error telemetry: HMAC-signed ingestion + public ingestion + session key.
    ("POST", "/api/v1/errors/ingest/public"): "public error ingestion (telemetry)",
    # Onboarding: creation-only + user onboarding actions (ADR creation-only rule).
    ("POST", "/api/v1/onboarding/actions/{action_id}/complete"): "onboarding user action",
    ("POST", "/api/v1/onboarding/actions/{action_id}/skip"): "onboarding user action",
    ("POST", "/api/v1/onboarding/dismiss"): "onboarding user action",
    # Remy: dev-mode-only assistant with its own session auth model.
    ("POST", "/api/v1/remy/sessions"): "Remy own session auth",
    ("PATCH", "/api/v1/remy/sessions/{session_id}"): "Remy own session auth",
    ("DELETE", "/api/v1/remy/sessions/{session_id}"): "Remy own session auth",
    ("POST", "/api/v1/remy/sessions/{session_id}/messages"): "Remy own session auth",
    ("POST", "/api/v1/remy/sessions/{session_id}/stream"): "Remy own session auth",
    ("POST", "/api/v1/remy/sessions/{session_id}/permission-response"): "Remy own session auth",
    ("POST", "/api/v1/remy/sessions/{session_id}/ui-command-results"): "Remy own session auth",
    ("POST", "/api/v1/remy/sessions/{session_id}/reset-permissions"): "Remy own session auth",
    ("POST", "/api/v1/remy/sessions/{session_id}/resume"): "Remy own session auth",
    ("POST", "/api/v1/remy/sessions/{session_id}/stop"): "Remy own session auth",
    ("POST", "/api/v1/remy/sessions/{session_id}/undo"): "Remy own session auth",
    # me/*: the user's own profile (get_current_user is the correct gate).
    ("PUT", "/api/v1/me/settings"): "self-profile (get_current_user)",
    ("PUT", "/api/v1/me/password"): "self-profile (get_current_user)",
    ("POST", "/api/v1/me/remy/skills"): "self-profile (get_current_user)",
    ("PUT", "/api/v1/me/remy/skills/{skill_id}"): "self-profile (get_current_user)",
    ("DELETE", "/api/v1/me/remy/skills/{skill_id}"): "self-profile (get_current_user)",
    ("PUT", "/api/v1/me/remy/context-sources/{source_key}"): "self-profile (get_current_user)",
    ("DELETE", "/api/v1/me/remy/context-sources"): "self-profile (get_current_user)",
    # Libraries: creation-only + user content + community (ADR creation-only rule).
    ("POST", "/api/v1/libraries/{primitive_id}/ratings"): "user content",
    ("POST", "/api/v1/libraries/{primitive_id}/ratings/abuse"): "user content",
    (
        "POST",
        "/api/v1/libraries/admin/library/community/publish/{primitive_id}",
    ): "community publish (admin-gated in handler)",
    # OAuth protocol: handled by A1b (client create/delete gated via oauth.client.*).
    ("POST", "/api/v1/mcp/oauth/clients"): "OAuth A1b flow",
    ("DELETE", "/api/v1/mcp/oauth/clients/{client_id}"): "OAuth A1b flow",
    ("POST", "/api/v1/mcp/oauth/consent/approve"): "OAuth A1b consent (get_current_tenant_user)",
    # Product analytics: self-service consent (accept/decline/dismiss) for the
    # current user's own org — get_current_tenant_user is the correct gate.
    (
        "POST",
        "/api/v1/org/product-analytics/consent",
    ): "product analytics self-service consent (get_current_tenant_user)",
    # Model backend setup flow.
    ("POST", "/api/v1/model-backends/{backend_id}/complete-setup"): "setup flow",
    # Pipeline from template: creation-only.
    ("POST", "/api/v1/pipelines/from-template/{template_id}"): "creation-only (ADR 3.4)",
    # auth: no principal yet at these endpoints.
    ("POST", "/api/v1/auth/login"): "auth (no principal)",
    ("POST", "/api/v1/auth/refresh"): "auth (no principal)",
    ("POST", "/api/v1/auth/logout"): "auth (refresh-token auth)",
    ("POST", "/api/v1/auth/saml/acs"): "SAML ACS (IdP session)",
    # Composite templates: creation/update/delete/publish routes are now
    # permission-tagged (pipeline.*); only stateless detect-params remains exempt.
    ("POST", "/api/v1/pipelines/{pipeline_id}/save-as-composite"): "composite creation-only (ADR 3.6)",
    ("POST", "/api/v1/composite-templates/detect-params"): "composite creation-only (ADR 3.6)",
    # Runs: node observe/recover carry inline admin/operator checks in the handler.
    ("POST", "/api/v1/runs/{run_id}/nodes/{node_id}/observe"): "inline admin/operator check in handler",
    ("POST", "/api/v1/runs/{run_id}/nodes/{node_id}/recover"): "inline admin/operator check in handler",
    # Runs: guardrail-override carries an inline admin/operator check in the handler.
    ("POST", "/api/v1/runs/{run_id}/guardrail-override"): "inline admin/operator check in handler",
    # Webhooks: HMAC/shared-secret is the authorization (exempt channel).
    ("POST", "/api/v1/triggers/{trigger_id}/webhook"): "HMAC/shared-secret channel",
    # Stripe: Stripe-Signature (HMAC-SHA256 of the raw body with the webhook
    # secret) is the authorization (exempt channel, mirrors the trigger webhook).
    ("POST", "/api/v1/webhooks/stripe"): "Stripe-Signature HMAC channel",
    (
        "POST",
        "/api/v1/triggers/{trigger_id}/webhook/replay/{event_id}",
    ): "HMAC/shared-secret channel (runner-or-HMAC in handler)",
    # Slack: X-Slack-Signature (HMAC-SHA256 with the trigger signing secret)
    # is the authorization (exempt channel, mirrors the webhook route).
    ("POST", "/api/v1/triggers/{trigger_id}/slack"): "X-Slack-Signature HMAC channel",
    # admin.py user/team/publisher/purge/retention routes: inline org_role==admin
    # checks in the handler (gated but not tagged). Tracked for conversion to
    # require_permission dependencies in a follow-up sweep.
    ("POST", "/api/v1/admin/users"): "admin.py inline _require_admin",
    ("POST", "/api/v1/admin/users/{user_id}/deactivate"): "admin.py inline _require_admin",
    ("POST", "/api/v1/admin/users/{user_id}/reactivate"): "admin.py inline _require_admin",
    ("POST", "/api/v1/admin/users/{user_id}/reset-password"): "admin.py inline _require_admin",
    ("PUT", "/api/v1/admin/users/{user_id}"): "admin.py inline _require_admin",
    ("POST", "/api/v1/admin/teams"): "admin.py inline _require_admin",
    ("PUT", "/api/v1/admin/teams/{team_id}"): "admin.py inline _require_admin",
    ("DELETE", "/api/v1/admin/teams/{team_id}"): "admin.py inline _require_admin",
    ("POST", "/api/v1/admin/publishers"): "admin.py inline _require_admin",
    ("PUT", "/api/v1/admin/publishers/{publisher_id}"): "admin.py inline _require_admin",
    ("DELETE", "/api/v1/admin/publishers/{publisher_id}"): "admin.py inline _require_admin",
    ("POST", "/api/v1/admin/purge/runs"): "admin.py inline _require_admin",
    ("POST", "/api/v1/admin/purge"): "admin.py inline _require_admin",
    ("POST", "/api/v1/admin/runs/purge"): "admin.py inline org_role==admin",
    ("PUT", "/api/v1/admin/runs/retention"): "admin.py inline org_role==admin",
    # Team member management: inline admin-OR-team-operator check in handler
    # (a team operator may manage their own team's members).
    ("POST", "/api/v1/teams/{team_id}/members"): "inline admin-or-team-operator check",
    ("DELETE", "/api/v1/teams/{team_id}/members/{membership_id}"): "inline admin-or-team-operator check",
    ("PATCH", "/api/v1/teams/{team_id}/members/{membership_id}"): "inline admin-or-team-operator check",
    ("PUT", "/api/v1/admin/org"): "admin.py inline _require_org_admin",
    ("PUT", "/api/v1/admin/org/sandbox-concurrency"): "admin.py inline _require_org_admin",
    ("PUT", "/api/v1/admin/org/run-concurrency"): "admin.py inline _require_org_admin",
    ("POST", "/api/v1/admin/org/regenerate-api-key"): "admin.py inline _require_org_admin",
}


def _route_key(route) -> tuple[str, str]:
    methods = sorted((route.methods or set()) - {"HEAD", "OPTIONS"})
    return (methods[0] if methods else "?", route.path)


def test_every_mutating_route_is_tagged_or_exempt() -> None:
    """Bidirectional map: each mutating route is tagged or exempt; nothing both."""
    mutating = get_mutating_routes(app)
    untagged = []
    for route in mutating:
        key = _route_key(route)
        tag = get_permission_tag(route)
        in_exempt = key in EXEMPT
        if tag is None and not in_exempt:
            untagged.append(f"{key[0]} {key[1]}")
        elif tag is not None and in_exempt:
            # Tagged AND exempt = contradiction — must be one or the other.
            pytest.fail(f"Route {key} is both tagged and in the exempt allowlist: {tag}")

    assert not untagged, "Mutating routes without a permission tag or exemption:\n" + "\n".join(untagged)


def test_exempt_allowlist_entries_exist() -> None:
    """Every exempt entry must actually be a mutating route (no stale entries)."""
    mutating_keys = {_route_key(r) for r in get_mutating_routes(app)}
    missing = [k for k in EXEMPT if k not in mutating_keys]
    assert not missing, "Exempt entries that are not mutating routes:\n" + "\n".join(str(m) for m in missing)


def test_permission_keys_resolve_in_registry() -> None:
    """Every tagged permission key resolves in PERMISSIONS (no typos)."""
    tagged = 0
    for route in get_mutating_routes(app):
        tag = get_permission_tag(route)
        if tag:
            tagged += 1
            resolved = get_resolved_min_role(tag["permission"])  # raises if key missing
            assert resolved, f"empty min-role for permission {tag['permission']} on {route.path}"
    assert tagged > 0, "no tagged mutating routes found — introspection sweep is vacuous"


def test_variant_kinds() -> None:
    """Variant-kind distribution sanity: org-scoped CRUD uses tenant, run
    trigger/status use tenant_or_api_key, admin strict uses system."""
    kinds = Counter()
    for route in get_mutating_routes(app):
        tag = get_permission_tag(route)
        if tag:
            kinds[tag["permission_kind"]] += 1
    # The three expected kinds must be present.
    assert "tenant" in kinds, f"no tenant-kind routes; got {dict(kinds)}"
    assert "tenant_or_api_key" in kinds, f"no tenant_or_api_key routes; got {dict(kinds)}"
    assert "system" in kinds, f"no system-kind routes; got {dict(kinds)}"


def test_scoped_hybrid_min_roles() -> None:
    """admin_email/admin_orgs scoped-hybrid routes carry the right min_role."""
    for route in get_mutating_routes(app):
        tag = get_permission_tag(route)
        if tag and tag["permission_kind"] == "scoped_hybrid":
            perm = tag["permission"]
            assert perm in (
                "org.email.view",
                "org.email.manage",
                "org.license.view",
                "org.license.manage",
                "org.authz_enforce.manage",
                "org.triggers.pause.manage",
                "org.guardrails.kill_switch.manage",
            ), f"unexpected scoped-hybrid permission {perm} on {route.path}"
            if perm in ("org.email.view", "org.license.view"):
                assert tag["min_role"] == "operator", f"{perm} should be operator, got {tag['min_role']}"
            else:
                assert tag["min_role"] == "admin", f"{perm} should be admin, got {tag['min_role']}"


def test_run_trigger_status_use_any_credential() -> None:
    """API-key REST triggering (PRD 5.2) uses the any-credential variant.

    Only the explicit trigger/status endpoints accept `mk_` keys over REST;
    `ws-token` (which reuses `run.status`) is a user-JWT mint endpoint and
    stays on the tenant variant.
    """
    api_key_paths = {
        "/api/v1/runs",
        "/api/v1/runs/{run_id}",
    }
    for route in get_mutating_routes(app):
        tag = get_permission_tag(route)
        if tag and tag["permission"] in ("run.trigger", "run.status") and route.path in api_key_paths:
            assert tag["permission_kind"] == "tenant_or_api_key", (
                f"{route.path} should use tenant_or_api_key, got {tag['permission_kind']}"
            )


def test_team_membership_gate_present_on_team_pipeline_routes() -> None:
    """PATCH/DELETE/graph on pipelines carry the team-scope gate."""
    for route in get_mutating_routes(app):
        if route.path in (
            "/api/v1/pipelines/{pipeline_id}",
            "/api/v1/pipelines/{pipeline_id}/graph",
        ):
            tag = get_permission_tag(route)
            assert tag is not None, f"{route.path} missing permission tag"
            kinds = {t["permission_kind"] for t in tag.get("tags", [tag])}
            assert "team_scope" in kinds, f"{route.path} missing team_scope gate; tags={tag.get('tags')}"


def test_no_duplicate_route_registrations() -> None:
    """Every (method, path) is registered exactly once.

    A route stacked with two identical ``@router.<method>(...)`` decorators
    (e.g. around ``@handle_db_errors``) registers twice. Starlette matches the
    FIRST-registered handler, so the raw, unwrapped function wins and the
    ``handle_db_errors`` wrapper becomes dead code — plus the route is
    duplicated in the OpenAPI schema. Previously fixed in
    admin_monitor_config.py / admin_triggers.py; the same defect was removed
    from events.py, hitl.py, and run_ws.py.
    """
    keys: dict[tuple[str, str], str] = {}
    duplicates: list[tuple[str, str]] = []

    for route in get_all_apiroutes(app):
        for method in sorted((route.methods or set()) - {"HEAD", "OPTIONS"}):
            key = (method, route.path)
            if key in keys:
                duplicates.append(key)
            else:
                keys[key] = route.path

    assert not duplicates, (
        "Routes registered more than once (first registration wins, so any "
        "error-handling wrapper on the second is dead code):\n" + "\n".join(f"{m} {p}" for m, p in duplicates)
    )


def test_handle_db_errors_wrapper_active_on_regexposed_routes() -> None:
    """The re-exposed HITL/SSE/WS handlers keep the handle_db_errors wrapper.

    Locking the fix from the double-registration cleanup: each previously
    duplicated route must now resolve to the *wrapped* endpoint (the wrapper
    sets ``__wrapped__``), not the raw handler that the old first-registration
    used to shadow.
    """
    wrapped: dict[str, str] = {}

    def _collect(routes: object, prefix: str = "") -> None:
        for r in getattr(routes, "routes", []):
            tn = type(r).__name__
            if tn in ("APIRoute", "APIWebSocketRoute") or "WebSocketRoute" in tn:
                path = (prefix + r.path) if prefix else r.path
                endpoint = getattr(r, "endpoint", None)
                if endpoint is not None and hasattr(endpoint, "__wrapped__"):
                    wrapped[path] = endpoint.__name__
            elif tn == "_IncludedRouter" and hasattr(r, "original_router"):
                _collect(r.original_router, prefix)

    _collect(app)

    expected = {
        "/api/v1/events",
        "/api/v1/runs/{run_id}/hitl/{gate_id}/claim",
        "/api/v1/runs/{run_id}/hitl/{gate_id}/approve",
        "/api/v1/runs/{run_id}/hitl/{gate_id}/approve-with-modification",
        "/api/v1/runs/{run_id}/hitl/{gate_id}/reject",
        "/api/v1/runs/{run_id}/hitl/{gate_id}/deliver-manual",
        "/api/v1/runs/{run_id}/manual/{gate_id}/submit",
        "/api/v1/runs/{run_id}/hitl/pending",
        "/api/v1/hitl/pending",
    }
    missing = sorted(p for p in expected if p not in wrapped)
    assert not missing, f"handle_db_errors wrapper not active on: {missing}"
    ws = [p for p, _ in wrapped.items() if p.endswith("/ws")]
    assert ws, "websocket route should carry the handle_db_errors wrapper"
