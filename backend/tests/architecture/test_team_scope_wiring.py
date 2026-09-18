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
