"""Shared route-introspection helpers for the ADR 017 permission sweep.

These helpers walk the FastAPI router and extract permission-dependency tags so
the strict introspection test can assert that every mutating user-principal
route carries an appropriate permission dependency (task-authz-b-sweep).
"""

from __future__ import annotations


def get_all_apiroutes(app: object) -> list:
    """Extract all APIRoute instances, including nested included routers.

    FastAPI >= 0.116 wraps included routers in ``_IncludedRouter`` entries that
    hide their routes behind ``original_router``; recurse into those. Skip
    Mount/WebSocket routes.
    """
    routes: list = []
    for r in app.routes:
        tn = type(r).__name__
        if tn == "APIRoute":
            routes.append(r)
        elif tn == "_IncludedRouter" and hasattr(r, "original_router"):
            sub_router = r.original_router
            if hasattr(sub_router, "routes"):
                for sr in sub_router.routes:
                    if type(sr).__name__ == "APIRoute":
                        routes.append(sr)
    return routes


def get_mutating_routes(app: object) -> list:
    """Return routes with mutating HTTP methods (POST/PUT/PATCH/DELETE)."""
    out = []
    for route in get_all_apiroutes(app):
        methods = route.methods or set()
        if methods & {"POST", "PUT", "PATCH", "DELETE"}:
            out.append(route)
    return out


def get_permission_tag(route) -> dict | None:
    """Find the permission-dependency tag(s) on a route, if any.

    The ``_tagged_dep`` helper sets ``permission``/``permission_kind``/``min_role``
    on the ``Depends`` object that becomes a route endpoint parameter default.
    Inspect the endpoint signature's parameter defaults for a ``Depends`` whose
    ``permission`` attribute is set. Multiple permission deps on one endpoint
    (e.g. ``require_permission`` + ``require_team_membership_or_admin``) are
    returned via the ``tags`` key; the primary tag is the first org-role one.
    """
    import inspect

    endpoint = getattr(route, "endpoint", None)
    if endpoint is None:
        return None
    try:
        sig = inspect.signature(endpoint)
    except (TypeError, ValueError):
        return None
    tags = []
    for param in sig.parameters.values():
        default = param.default
        if type(default).__name__ == "Depends":
            permission = getattr(default, "permission", None)
            if permission is not None:
                tags.append(
                    {
                        "permission": permission,
                        "permission_kind": getattr(default, "permission_kind", None),
                        "min_role": getattr(default, "min_role", None),
                    }
                )
    if not tags:
        return None
    primary = next(
        (
            t
            for t in tags
            if t["permission_kind"] in ("tenant", "tenant_or_api_key", "scoped_hybrid", "system", "system_or_org")
        ),
        tags[0],
    )
    return {**primary, "tags": tags}


def get_resolved_min_role(permission: str) -> str:
    """Resolve the minimum org role for a permission key from the registry."""
    from modulo.auth.permissions import resolve_required

    return resolve_required(permission)
