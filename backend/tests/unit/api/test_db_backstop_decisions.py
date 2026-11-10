"""FAR-1912: the per-route error-backstop decision is recorded and enforced.

The ``@handle_db_errors`` convention is deliberately not universal: several
DB-reaching routes carry a complete hand-written arm chain, some delegate to a
shared wrapper that applies the same contract, and a couple of surfaces must
deviate (a settings READ degrades to its cache; a streaming export's DB work
runs after the handler returns). FAR-1912 required that decision to be taken
route by route and RECORDED, not left as a silent gap.

Each of the four route modules therefore carries a module-level
``_DB_BACKSTOP_DECISION`` map from route function name to decision. This test
makes that record structural rather than prose:

* every route on the router has a row (a new route cannot be added silently);
* every row names a route that still exists (no stale claims);
* every decision comes from the fixed vocabulary;
* ``backstop`` <=> the route actually carries ``@handle_db_errors``;
* ``no_db`` routes genuinely take no DB session / backstop / DB wrapper;
* ``local_contract`` routes genuinely contain the session-contract
  classification (the arm that makes them equivalent to the backstop for the
  FAR-1408 class), and are NOT decorated (the decorator would be inert).
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from farna.api.routes import admin, admin_run_retention, observability, product_analytics_identity

_VOCABULARY = frozenset(
    {
        "backstop",
        "shared_helper",
        "local_contract",
        "degraded_read",
        "streaming_excluded",
        "no_db",
    }
)

#: Shared wrappers that already apply the full error contract.
_DB_HELPERS = ("_run_admin_rls_txn", "_admin_journey_write", "_load_org_retention_setting")

_MODULES = {
    "admin": admin,
    "admin_run_retention": admin_run_retention,
    "observability": observability,
    "product_analytics_identity": product_analytics_identity,
}


class _Route:
    """One route's statically-extracted facts."""

    def __init__(self, node: ast.AsyncFunctionDef, source: str) -> None:
        self.name = node.name
        self.decorators = [ast.unparse(d) for d in node.decorator_list]
        self.source = ast.get_source_segment(source, node) or ""
        self._args = ast.unparse(node.args)
        self.has_session_dep = "get_db_session" in self._args
        self.has_backstop = any(decorator.startswith("handle_db_errors(") for decorator in self.decorators)
        self.uses_db_helper = any(helper in self.source for helper in _DB_HELPERS)
        self.has_session_contract_guard = "raise_session_contract_error" in self.source


def _routes() -> dict[str, list[_Route]]:
    """Extract every route (a function decorated with ``@router.<verb>``)."""
    extracted: dict[str, list[_Route]] = {}
    for module_name, module in _MODULES.items():
        assert module.__file__ is not None
        source = pathlib.Path(module.__file__).read_text()
        tree = ast.parse(source)
        routes: list[_Route] = []
        for node in tree.body:
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            decorators = [ast.unparse(d) for d in node.decorator_list]
            if any(decorator.startswith("router.") for decorator in decorators):
                routes.append(_Route(node, source))
        extracted[module_name] = routes
    return extracted


_ROUTES = _routes()


@pytest.mark.parametrize("module_name", sorted(_MODULES))
def test_every_route_is_recorded(module_name: str) -> None:
    """A route without a recorded decision is the silent gap FAR-1912 closes."""
    decision = _MODULES[module_name]._DB_BACKSTOP_DECISION
    route_names = {route.name for route in _ROUTES[module_name]}
    assert route_names, f"{module_name} exposes no routes — the extractor broke"
    missing = route_names - set(decision)
    assert not missing, f"{module_name} routes with no recorded backstop decision: {sorted(missing)}"
    stale = set(decision) - route_names
    assert not stale, f"{module_name} decision map names routes that no longer exist: {sorted(stale)}"


@pytest.mark.parametrize("module_name", sorted(_MODULES))
def test_every_decision_is_in_the_vocabulary(module_name: str) -> None:
    decision = _MODULES[module_name]._DB_BACKSTOP_DECISION
    unknown = {name: value for name, value in decision.items() if value not in _VOCABULARY}
    assert not unknown, f"{module_name} uses decisions outside the documented vocabulary: {unknown}"


@pytest.mark.parametrize("module_name", sorted(_MODULES))
def test_backstop_rows_match_the_decorator(module_name: str) -> None:
    """The record cannot lie in either direction."""
    decision = _MODULES[module_name]._DB_BACKSTOP_DECISION
    by_name = {route.name: route for route in _ROUTES[module_name]}
    for name, verdict in decision.items():
        route = by_name[name]
        if verdict == "backstop":
            assert route.has_backstop, f"{module_name}.{name} is recorded as 'backstop' but carries no decorator"
        else:
            assert not route.has_backstop, (
                f"{module_name}.{name} carries @handle_db_errors but is recorded as {verdict!r} — "
                "the decision map and the code disagree"
            )


@pytest.mark.parametrize("module_name", sorted(_MODULES))
def test_no_db_rows_really_do_not_touch_the_database(module_name: str) -> None:
    decision = _MODULES[module_name]._DB_BACKSTOP_DECISION
    by_name = {route.name: route for route in _ROUTES[module_name]}
    for name, verdict in decision.items():
        if verdict != "no_db":
            continue
        route = by_name[name]
        assert not route.has_session_dep, f"{module_name}.{name} is recorded 'no_db' but takes the DB session"
        assert not route.has_backstop, f"{module_name}.{name} is recorded 'no_db' but carries the backstop"
        assert not route.uses_db_helper, f"{module_name}.{name} is recorded 'no_db' but delegates to a DB wrapper"


@pytest.mark.parametrize("module_name", sorted(_MODULES))
def test_local_contract_rows_reproduce_the_contract(module_name: str) -> None:
    """``local_contract`` is only honest if the arm chain classifies the FAR-1408 class.

    A local ``except Exception`` arm shadows ``@handle_db_errors``, which is why
    those routes are not wrapped — so their own chain must contain the
    session-contract guard (a contract violation is a 500, never a 503 outage).
    """
    decision = _MODULES[module_name]._DB_BACKSTOP_DECISION
    by_name = {route.name: route for route in _ROUTES[module_name]}
    for name, verdict in decision.items():
        if verdict != "local_contract":
            continue
        route = by_name[name]
        assert route.has_session_contract_guard, (
            f"{module_name}.{name} is recorded 'local_contract' but has no raise_session_contract_error guard"
        )


def test_streaming_export_is_recorded_as_excluded_and_unwrapped() -> None:
    """The streaming export decision is explicit, not an accidental gap."""
    decision = admin_run_retention._DB_BACKSTOP_DECISION
    assert decision["export"] == "streaming_excluded"
    by_name = {route.name: route for route in _ROUTES["admin_run_retention"]}
    assert not by_name["export"].has_backstop
