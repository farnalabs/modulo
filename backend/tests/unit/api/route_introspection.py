"""Shared route-introspection helpers for the ADR 047 permission sweep.

These helpers walk the FastAPI router and extract the authorisation metadata
the strict introspection tests need to assert the route guards
(``test_route_introspection.py``). Two kinds of metadata are collected:

* a **permission tag** — a ``Depends`` built by one of the ``require_*``
  factories in ``modulo.api.dependencies`` (``_tagged_dep`` stamps
  ``permission`` / ``permission_kind`` / ``min_role`` on it). A tag may be
  declared on the endpoint signature, on the route decorator, merged from an
  ``APIRouter(dependencies=[...])`` / ``include_router(..., dependencies=[...])``,
  or nested inside another dependency — **all of them are collected**
  (``get_permission_tag`` -> ``_iter_applied_depends``). Reading only the
  endpoint signature (the pre-FAR-1969 behaviour) made every route-level gate
  invisible.
* the **dependency callables** applied to a route (``get_applied_dependency_names``),
  which is how a read route can be shown to be authenticated by a
  principal-bearing dependency rather than by a permission tag.

FastAPI stores route-level ``Depends`` objects on ``route.dependencies`` but
rebuilds them as ``Dependant`` nodes that do **not** carry the introspection
tag, so the raw ``route.dependencies`` list must be read directly — the
``Dependant`` tree alone cannot see a route-level permission gate.
"""

from __future__ import annotations

import typing


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
                routes.extend(sr for sr in sub_router.routes if type(sr).__name__ == "APIRoute")
    return routes


def get_mutating_routes(app: object) -> list:
    """Return routes with mutating HTTP methods (POST/PUT/PATCH/DELETE)."""
    out = []
    for route in get_all_apiroutes(app):
        methods = route.methods or set()
        if methods & {"POST", "PUT", "PATCH", "DELETE"}:
            out.append(route)
    return out


def get_read_routes(app: object) -> list:
    """Return routes that serve a GET method (the read surface).

    Reads were outside the introspection guard before FAR-1969, so a read
    route could be added without any authorisation and nothing failed.
    """
    out = []
    for route in get_all_apiroutes(app):
        if "GET" in (route.methods or set()):
            out.append(route)
    return out


def _deps_from_callable(fn: object) -> list:
    """Return every ``Depends`` applied as a parameter of ``fn``.

    Two FastAPI patterns are supported:

    * classic default-value form —
      ``user: T = Depends(require_system_permission("..."))`` — where the
      ``Depends`` is the parameter *default*;
    * ``Annotated`` form (ADR 047 sweep, used to drop ``# type: ignore``) —
      ``user: Annotated[T, Depends(require_system_permission("..."))]`` — where
      the ``Depends`` lives in the annotation metadata rather than the default.
      ``include_extras=True`` is essential: under ``from __future__ import
      annotations`` the ``Depends`` lives in an ``Annotated`` metadata slot,
      and default ``inspect.signature`` resolution strips those extras.

    Returns ``[]`` (fail-soft) when the callable's signature cannot be
    introspected; the route is still covered by the other declaration sites.
    """
    import inspect

    out: list = []
    try:
        sig = inspect.signature(fn)  # type: ignore[arg-type]
        hints = typing.get_type_hints(fn, include_extras=True)
    except (TypeError, ValueError, NameError):
        return out
    for name, param in sig.parameters.items():
        default = getattr(param, "default", None)
        if type(default).__name__ == "Depends":
            out.append(default)
        annotation = hints.get(name, param.annotation)
        for meta in getattr(annotation, "__metadata__", ()):
            if type(meta).__name__ == "Depends":
                out.append(meta)
    return out


def _iter_applied_depends(route) -> list:
    """Every ``Depends`` applied to a route, wherever it is declared.

    Three declaration sites are covered, so a permission gate cannot hide
    behind any of them:

    * the endpoint signature (``param: T = Depends(...)`` / ``Annotated``);
    * ``route.dependencies`` — dependencies declared on the route decorator,
      or merged from ``APIRouter(dependencies=[...])`` /
      ``include_router(..., dependencies=[...])``;
    * the dependency tree (``route.dependant.dependencies``), walked
      recursively so a tagged ``Depends`` nested inside another dependency is
      found too.

    ``route.dependencies`` is load-bearing: FastAPI copies route-level
    ``Depends`` objects into ``route.dependencies`` but rebuilds them as
    ``Dependant`` nodes that do **not** carry the introspection tag, so the tag
    on a route-level gate is only visible on the raw ``Depends``.
    """
    found: list = []
    seen: set[int] = set()

    def add(dep: object) -> None:
        if type(dep).__name__ != "Depends" or id(dep) in seen:
            return
        seen.add(id(dep))
        found.append(dep)

    endpoint = getattr(route, "endpoint", None)
    if endpoint is not None:
        for dep in _deps_from_callable(endpoint):
            add(dep)
    for dep in getattr(route, "dependencies", None) or []:
        add(dep)

    def walk(dependant: object) -> None:
        call = getattr(dependant, "call", None)
        if call is not None and call is not endpoint:
            for dep in _deps_from_callable(call):
                add(dep)
        for sub in getattr(dependant, "dependencies", None) or []:
            walk(sub)

    root = getattr(route, "dependant", None)
    if root is not None:
        walk(root)
    return found


def get_permission_tag(route) -> dict | None:
    """Find the permission-dependency tag(s) on a route, if any.

    The ``_tagged_dep`` helper sets ``permission``/``permission_kind``/``min_role``
    on a ``Depends`` object. Every ``Depends`` applied to the route is inspected
    — endpoint signature, route/router-level ``dependencies=[...]``, and nested
    dependencies (see ``_iter_applied_depends``). Multiple permission deps on one
    route (e.g. ``require_permission`` + ``require_team_membership_or_admin``)
    are returned via the ``tags`` key; the primary tag is the first org-role one.
    """
    tags = []
    for dep in _iter_applied_depends(route):
        if getattr(dep, "permission", None) is None:
            continue
        tags.append(
            {
                "permission": dep.permission,
                "permission_kind": getattr(dep, "permission_kind", None),
                "min_role": getattr(dep, "min_role", None),
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


def get_applied_dependency_names(route) -> set[str]:
    """Names of every dependency callable applied to a route.

    Used by the read-route guard to assert a route is authenticated by a
    principal-bearing dependency even without a permission tag
    (``test_route_introspection.py``).
    """
    names: set[str] = set()
    for dep in _iter_applied_depends(route):
        fn = getattr(dep, "dependency", None)
        if fn is not None:
            names.add(getattr(fn, "__name__", repr(fn)))
    return names


def get_resolved_min_role(permission: str) -> str:
    """Resolve the minimum org role for a permission key from the registry."""
    from modulo.auth.permissions import resolve_required

    return resolve_required(permission)
