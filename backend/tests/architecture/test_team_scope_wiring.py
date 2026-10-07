"""ADR 038 team-gate wiring assertion.

Every SQLAlchemy model that carries BOTH ``owner_team_id`` and ``visibility``
must appear in ``TEAM_SCOPED_RESOLVERS`` — otherwise its routes silently have
no team-membership gate, breaking RLS parity.

Models with ``owner_team_id`` but NO ``visibility`` column are on an explicit
allowlist, each with a one-line reason referencing ADR 038: these are
org-role-floor-only because access derives from their parent entity.

Convention: the resolver key is the model's ``__tablename__`` (e.g.
``Pipeline.__tablename__ == "pipelines"`` maps to key ``"pipelines"``).

When adding a new team-scoped model:

1. Add the resolver to ``modulo.api.team_scope`` (``resolve_<name>_team_scope``).
2. Add it to ``TEAM_SCOPED_RESOLVERS`` keyed by ``__tablename__``.
3. The tests below pick it up automatically from the model's columns.

Models with ``owner_team_id`` and no ``visibility`` must instead be added to
``MODELS_WITH_OWNER_TEAM_ID_NO_VISIBILITY`` with a one-line reason;
``test_every_owner_team_model_is_accounted_for`` fails either way.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import DeclarativeBase

from modulo.api.team_scope import TEAM_SCOPED_RESOLVERS
from modulo.db.models import (
    Journey,
    Run,
    SuiteRun,
)

# ---------------------------------------------------------------------------
# 1. Model → resolver-key mapping convention
# ---------------------------------------------------------------------------
# The resolver key IS the model's __tablename__.  This is not a coincidence:
# every team-scoped table was named to match its resolver key at creation time,
# and the test enforces this going forward.

# Registry: SQLAlchemy model class → expected resolver key.
# Update this map when a NEW model gains both owner_team_id and visibility.
MODEL_TO_RESOLVER_KEY: dict[type[Any], str] = {}


def _collect_team_scoped_models() -> None:
    """Populate MODEL_TO_RESOLVER_KEY by scanning all declarative models."""
    from modulo.db.models import Base

    assert issubclass(Base, DeclarativeBase)
    for mapper in Base.registry.mappers:
        model = mapper.class_
        columns = {c.key for c in mapper.column_attrs}
        if "owner_team_id" in columns and "visibility" in columns:
            MODEL_TO_RESOLVER_KEY[model] = model.__tablename__


_collect_team_scoped_models()


# ---------------------------------------------------------------------------
# 2. Allowlist: models with owner_team_id but NO visibility column
# ---------------------------------------------------------------------------
# Each entry: (model_class, reason).  These are intentionally on the org-role
# floor — access derives from their parent entity (ADR 038 Decision §5).
MODELS_WITH_OWNER_TEAM_ID_NO_VISIBILITY: list[tuple[type[Any], str]] = [
    (Run, "Run access derives from pipeline ownership (ADR 038 §5); owner_team_id is metadata, not a security control"),
    (SuiteRun, "SuiteRun access derives from EvalSuite ownership (ADR 038 §5); no own visibility column"),
    (Journey, "Journey access derives from its parent run/pipeline (ADR 038 §5); org-role-floor-only"),
]

# ---------------------------------------------------------------------------
# 3. Route wiring: who carries the request-time gate, who is DB-RLS-only
# ---------------------------------------------------------------------------
# FAR-1514 DECISION, recorded here as well as in ``api.team_scope`` so a future
# sweep cannot "finish" it by accident:
#
# * ``_DB_RLS_ONLY_UNWIRED`` — resolver registered in TEAM_SCOPED_RESOLVERS
#   (complete registry) but deliberately NOT attached to any route dependency.
#   DB RLS (``rls_team_isolation``, sole policy since 0109/0110 + 0124) is
#   their enforcement; wiring them would add a redundant second transaction +
#   membership query per request, not close a gap.
# * ``_LIFECYCLE_MAP_GATED_ENDPOINTS`` — the ``{lifecycle_map_id}``-scoped
#   endpoints that MUST carry the gate (FAR-1514). The three endpoints with no
#   id path param (list/create/import) must NOT: the path-param resolver would
#   find no id and the gate would 404 every call.
_DB_RLS_ONLY_UNWIRED: frozenset[str] = frozenset(
    {
        "connector_instances",
        "model_backends",
        "environment_profiles",
        "library_primitives",
    }
)

#: ORM class per DB-RLS-only table — the second spelling a hand-written
#: resolver would use to read one (``ConnectorInstance`` vs the raw
#: ``connector_instances`` tablename).
_MODEL_CLASS_BY_TABLE: dict[str, str] = {
    "connector_instances": "ConnectorInstance",
    "model_backends": "ModelBackend",
    "environment_profiles": "EnvironmentProfile",
    "library_primitives": "LibraryPrimitive",
}

_LIFECYCLE_MAP_GATED_ENDPOINTS: tuple[str, ...] = (
    "export_lifecycle_map_endpoint",
    "get_lifecycle_map_endpoint",
    "update_lifecycle_map_endpoint",
    "delete_lifecycle_map_endpoint",
    "restore_lifecycle_map_endpoint",
    "list_lifecycle_map_versions_endpoint",
    "save_lifecycle_map_version_endpoint",
    "update_lifecycle_map_version_endpoint",
    "get_lifecycle_map_version_endpoint",
    "graduate_lifecycle_map_stage_endpoint",
    "list_journeys_endpoint",
    "get_journey_endpoint",
    "self_report_journeys_endpoint",
)

_LIFECYCLE_MAP_UNGATED_ENDPOINTS: tuple[str, ...] = (
    "list_lifecycle_maps_endpoint",
    "create_lifecycle_map_endpoint",
    "import_lifecycle_map_endpoint",
)


def _endpoint_has_team_scope_gate(endpoint: Any) -> bool:
    """True when any parameter default is a Depends tagged ``team_scope``."""
    import inspect

    for param in inspect.signature(endpoint).parameters.values():
        default = param.default
        if type(default).__name__ == "Depends" and getattr(default, "permission_kind", None) == "team_scope":
            return True
    return False


def _table_read_by_resolver(provider: Any) -> str | None:
    """The team-scoped table a resolver reads, or ``None`` when not forbidden.

    Returns one of ``_DB_RLS_ONLY_UNWIRED`` when the resolver reads a
    DB-RLS-only table (i.e. wiring it on a route violates the FAR-1514
    DECISION); ``None`` when the resolver is known NOT to. Detection order:

    1. the ``model`` closure cell — every registry resolver is built by
       ``team_scope_resolver``, whose inner ``_resolve`` closes over the model
       (all of them share the internal name ``_resolve``, so names cannot
       discriminate);
    2. identity / name against ``TEAM_SCOPED_RESOLVERS`` — catches the literal
       ``resolve_connector_team_scope`` wiring this test names;
    3. the provider's own source, grepped for the four model classes /
       tablenames — catches a hand-written resolver that reads one of those
       tables directly (the deleted-inclusive variants and the trigger_run
       body resolver are hand-written and read ``pipelines``, so they come
       back ``None`` here).
    """
    import inspect

    try:
        model = inspect.getclosurevars(provider).nonlocals.get("model")
    except TypeError:
        model = None
    if isinstance(model, type):
        table = getattr(model, "__tablename__", None)
        if table in _DB_RLS_ONLY_UNWIRED:
            return str(table)
        return None
    for key, registered in TEAM_SCOPED_RESOLVERS.items():
        if registered is provider or provider.__name__ == key:
            return key if key in _DB_RLS_ONLY_UNWIRED else None
    try:
        source = inspect.getsource(provider)
    except (OSError, TypeError):
        return None
    for table in sorted(_DB_RLS_ONLY_UNWIRED):
        if table in source or _MODEL_CLASS_BY_TABLE[table] in source:
            return table
    return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTeamScopeWiring:
    """Every model with owner_team_id + visibility must be in TEAM_SCOPED_RESOLVERS."""

    def test_every_team_scoped_model_has_resolver(self) -> None:
        """Models with both owner_team_id and visibility appear in the registry."""
        missing: list[str] = []
        for model, expected_key in MODEL_TO_RESOLVER_KEY.items():
            if expected_key not in TEAM_SCOPED_RESOLVERS:
                missing.append(
                    f"{model.__name__} (tablename={model.__tablename__!r}) — expected resolver key {expected_key!r}"
                )
        assert not missing, "Models with owner_team_id + visibility but no TEAM_SCOPED_RESOLVERS entry:\n" + "\n".join(
            missing
        )

    def test_resolver_keys_match_tablenames(self) -> None:
        """Every resolver key in TEAM_SCOPED_RESOLVERS matches a model's __tablename__."""
        model_tablenames = {m.__tablename__: m for m in MODEL_TO_RESOLVER_KEY}
        unexpected = set(TEAM_SCOPED_RESOLVERS) - set(model_tablenames)
        assert not unexpected, f"TEAM_SCOPED_RESOLVERS has keys not matching any model __tablename__: {unexpected}"

    def test_allowlisted_models_have_no_visibility(self) -> None:
        """Each model on the owner_team_id-no-visibility allowlist truly lacks the column."""
        for model, _reason in MODELS_WITH_OWNER_TEAM_ID_NO_VISIBILITY:
            mapper = sa_inspect(model)
            columns = {c.key for c in mapper.column_attrs}
            assert "visibility" not in columns, (
                f"{model.__name__} is on the no-visibility allowlist but HAS a visibility column — "
                "remove it from MODELS_WITH_OWNER_TEAM_ID_NO_VISIBILITY and add it to MODEL_TO_RESOLVER_KEY"
            )
            assert "owner_team_id" in columns, (
                f"{model.__name__} is on the allowlist but lacks owner_team_id — inconsistent"
            )

    def test_allowlisted_models_are_not_in_resolver(self) -> None:
        """Allowlisted models must NOT also appear in the resolver (double-gating)."""
        for model, _reason in MODELS_WITH_OWNER_TEAM_ID_NO_VISIBILITY:
            key = model.__tablename__
            assert key not in TEAM_SCOPED_RESOLVERS, (
                f"{model.__name__} (key={key!r}) is both in TEAM_SCOPED_RESOLVERS and the "
                "no-visibility allowlist — pick one"
            )

    def test_expected_registry_matches_actual(self) -> None:
        """The TEAM_SCOPED_RESOLVERS keys exactly match the expected set on main.

        If a new model is added, this test will fail until it is either wired
        into TEAM_SCOPED_RESOLVERS (if it has visibility) or added to the
        allowlist (if it does not).
        """
        expected = {
            "pipelines",
            "connector_instances",
            "model_backends",
            "environment_profiles",
            "library_primitives",
            "lifecycle_maps",
            "eval_datasets",
            "eval_suites",
        }
        actual = set(TEAM_SCOPED_RESOLVERS)
        assert actual == expected, (
            f"TEAM_SCOPED_RESOLVERS mismatch:\n  unexpected: {actual - expected}\n  missing: {expected - actual}"
        )

    def test_every_owner_team_model_is_accounted_for(self) -> None:
        """Every model with ``owner_team_id`` is team-scoped or explicitly allowlisted.

        Closes the observability gap left by the two tests above: a future
        model carrying ``owner_team_id`` and no ``visibility`` that is added to
        neither structure would otherwise stay on the org-role floor silently.
        This makes that omission a test failure (not a security gap — the
        org-role floor still applies — but a missing-decision gap).
        """
        from modulo.db.models import Base

        allowlisted = {model.__tablename__ for model, _reason in MODELS_WITH_OWNER_TEAM_ID_NO_VISIBILITY}
        accounted_for = set(TEAM_SCOPED_RESOLVERS) | allowlisted
        missing: list[str] = []
        for mapper in Base.registry.mappers:
            model = mapper.class_
            columns = {c.key for c in mapper.column_attrs}
            if "owner_team_id" in columns and model.__tablename__ not in accounted_for:
                missing.append(f"{model.__name__} (tablename={model.__tablename__!r})")
        assert not missing, (
            "Models with owner_team_id that are in neither TEAM_SCOPED_RESOLVERS nor "
            "MODELS_WITH_OWNER_TEAM_ID_NO_VISIBILITY:\n" + "\n".join(missing)
        )


class TestRouteWiring:
    """The request-time gate on the routes FAR-1514 wired (and those it didn't)."""

    def test_lifecycle_map_id_routes_carry_team_gate(self) -> None:
        """Every ``{lifecycle_map_id}``-scoped endpoint has the team-scope gate.

        The resolver reads ``lifecycle_map_id`` from the path params, so the
        gate can only be attached where that param exists. A route here losing
        its ``team_scope`` tag (a refactor dropping the dependency) fails this
        test instead of silently exposing team-private maps to any org member.
        """
        import modulo.api.routes.lifecycle_maps as lifecycle_routes

        missing: list[str] = []
        for name in _LIFECYCLE_MAP_GATED_ENDPOINTS:
            endpoint = getattr(lifecycle_routes, name, None)
            if endpoint is None:
                missing.append(f"{name}: endpoint not found on api.routes.lifecycle_maps")
            elif not _endpoint_has_team_scope_gate(endpoint):
                missing.append(f"{name}: no require_team_membership_or_admin(...) dependency")
        assert not missing, "lifecycle_maps endpoints missing the team_scope gate:\n" + "\n".join(missing)

    def test_lifecycle_map_non_id_routes_have_no_team_gate(self) -> None:
        """List/create/import carry NO id-scoped gate — the resolver would 404 them.

        ``team_scope_resolver`` returns ``None`` when the path param is absent
        and ``require_team_membership_or_admin`` turns a ``None`` row into a
        404, so wiring these three would make them permanently unreachable.
        They stay on the org-role floor; the DB policy (0287) filters their
        result sets by visibility.
        """
        import modulo.api.routes.lifecycle_maps as lifecycle_routes

        wrongly_gated = [
            name
            for name in _LIFECYCLE_MAP_UNGATED_ENDPOINTS
            if _endpoint_has_team_scope_gate(getattr(lifecycle_routes, name))
        ]
        assert not wrongly_gated, (
            "lifecycle_maps endpoints without a {lifecycle_map_id} path param must NOT carry the "
            f"team_scope gate (the resolver would 404 every call): {wrongly_gated}"
        )

    def test_db_rls_only_tables_stay_registered_not_wired(self) -> None:
        """The four core tables keep their resolvers registered, never wired.

        Pins the FAR-1514 DECISION recorded in ``api/team_scope``: these four
        are enforced by DB RLS alone. Two halves, both must hold:

        * REGISTERED — their resolvers stay in ``TEAM_SCOPED_RESOLVERS``
          (registry completeness, the other tests assert the inverse direction);
        * NOT WIRED — no endpoint in ``api.routes`` binds a team-scope gate
          whose resolver reads one of those tables. The previous version of
          this test compared a literal set against an identical literal in the
          same file (a tautology) and never checked this second half, so
          wiring ``resolve_connector_team_scope`` onto a route would have
          passed it.

        The resolver is identified by the MODEL it reads (the ``model``
        closure cell of ``team_scope_resolver``'s inner function — every
        registry entry is built by that factory and shares the internal name
        ``_resolve``, so names cannot discriminate), falling back to registry
        identity and then to the resolver's own source. The walk also carries
        positive controls (it must FIND the gates it is meant to police), so a
        broken walk fails instead of passing vacuously.
        """
        import importlib
        import inspect
        import pkgutil

        import modulo.api.routes as routes_pkg

        seen_gates = 0
        lifecycle_gates = 0
        violations: list[str] = []
        undecidable: list[str] = []

        for mod_info in pkgutil.iter_modules(routes_pkg.__path__):
            module = importlib.import_module(f"{routes_pkg.__name__}.{mod_info.name}")
            for name, fn in inspect.getmembers(module, inspect.isfunction):
                if fn.__module__ != module.__name__:
                    continue  # imported helper, not an endpoint of this module
                for param in inspect.signature(fn).parameters.values():
                    default = param.default
                    if type(default).__name__ != "Depends":
                        continue
                    if getattr(default, "permission_kind", None) != "team_scope":
                        continue
                    seen_gates += 1
                    if mod_info.name == "lifecycle_maps":
                        lifecycle_gates += 1
                    try:
                        provider = inspect.getclosurevars(default.dependency).nonlocals.get("resource_team_id_provider")
                    except TypeError:
                        provider = None
                    if provider is None:
                        undecidable.append(f"{module.__name__}.{name}")
                        continue
                    table = _table_read_by_resolver(provider)
                    if table is not None:
                        violations.append(f"{module.__name__}.{name} binds a {table} team-scope resolver")

        assert not undecidable, (
            "A team_scope gate's resolver could not be extracted from the dependency "
            "closure — the walk cannot prove these stay unwired: " + "; ".join(undecidable)
        )
        assert seen_gates > 0, "no team_scope gates found — the route walk is broken"
        assert lifecycle_gates > 0, (
            "expected the lifecycle_maps team gates to be discovered by the walk "
            "(they are the FAR-1514 wiring); the walk is not seeing real endpoints"
        )
        assert not violations, (
            "DB-RLS-only tables must NOT be route-wired (the FAR-1514 DECISION "
            "recorded in api/team_scope): " + "; ".join(violations)
        )
        # Registered, never wired — both halves of the decision.
        assert set(TEAM_SCOPED_RESOLVERS) >= _DB_RLS_ONLY_UNWIRED

    def test_db_rls_only_tables_have_a_team_policy_in_the_migration(self) -> None:
        """Every DB-RLS-only table is covered by a team-policy migration.

        A table that is neither route-wired nor DB-policy'd would be a
        neither-layer hole — exactly what FAR-1514 closed for
        ``lifecycle_maps`` / ``eval_datasets`` / ``eval_suites``. Read the
        migration files as text (no DB needed) and require a
        ``CREATE POLICY rls_team_isolation ... {table}`` for each name.

        The ``CREATE POLICY`` prefix matters: the policy name alone also
        matches ``DROP POLICY IF EXISTS rls_team_isolation ON ...``, so the
        previous ``rls_team_isolation[^\\n]*{table}`` pattern reported coverage
        from the very statement that removes the policy. Live-state
        confirmation (the policy really exists in a migrated database) is
        ``tests/integration/test_rls_isolation.py::
        test_rls_team_isolation_policies_exist``; this test pins that the
        migration TREE creates it.
        """
        import re
        from pathlib import Path

        versions = Path(__file__).resolve().parents[2] / "src" / "modulo" / "db" / "migrations" / "versions"
        corpus = "\n".join(p.read_text(encoding="utf-8") for p in versions.glob("*.py"))
        missing = [
            t
            for t in sorted(_DB_RLS_ONLY_UNWIRED)
            if not re.search(rf"CREATE POLICY rls_team_isolation[^\n]*{t}", corpus)
        ]
        assert not missing, (
            "Tables enforced by DB RLS alone have no CREATE POLICY rls_team_isolation "
            f"anywhere in the migration tree: {missing}"
        )
