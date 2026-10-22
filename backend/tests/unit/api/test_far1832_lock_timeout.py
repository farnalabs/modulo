"""FAR-1832 QA F2: a bounded lock timeout (SQLSTATE 55P03) is a 409 conflict.

The org-settings patch bounds every converted writer's row-lock wait
(``set_mutation_row_lock_timeout``), so SQLSTATE 55P03 is now REACHABLE on the
admin / admin-orgs / product-analytics routes. Before F2 their local except
arms collapsed it into a generic 503 ("Database temporarily unavailable" —
which invites a client retry storm against the busy lock) or a 500. These
tests pin the shared classifier at each boundary:

- 55P03 -> 409 with the canonical "another change is in progress" detail;
- a genuine connection/unavailability error -> the pre-existing 503;
- the admin-orgs boundary keeps every other error at its pre-existing 503.

The real in-flight-lock-holder variants (real Postgres, real handlers) live in
``tests/integration/crud/test_org_settings_patch.py``.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from modulo.api import routes as _routes_package
from modulo.api.db_error_handling import raise_lock_timeout_conflict_if
from modulo.api.dependencies import get_db_session
from modulo.api.routes import product_analytics as pa_module
from modulo.api.routes.admin import _run_admin_rls_txn
from modulo.api.routes.product_analytics import post_consent
from modulo.auth.jwt import TenantPrincipal
from tests.unit.api.mock_session import configure_rls_preamble

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")

_CONSENT_URL = "/api/v1/org/product-analytics/consent"
_PRODUCT_ANALYTICS_LEVEL_URL = "/api/v1/org/product-analytics"

_LOCK_TIMEOUT_DETAIL = "another change is in progress"

app = FastAPI()
app.include_router(pa_module.router)


class _PgError(Exception):
    """Minimal driver-error double carrying a SQLSTATE (asyncpg ``sqlstate``)."""

    def __init__(self, state: str) -> None:
        super().__init__(f"driver error {state}")
        self.sqlstate = state


def _operational(state: str | None) -> OperationalError:
    orig = _PgError(state) if state else Exception("server closed the connection unexpectedly")
    return OperationalError("UPDATE organisations ...", {}, orig)


# ---------------------------------------------------------------------------
# The shared classifier itself
# ---------------------------------------------------------------------------


def test_classifier_maps_55p03_to_409() -> None:
    with pytest.raises(HTTPException) as exc_info:
        raise_lock_timeout_conflict_if(_operational("55P03"), "unit.classifier")
    assert exc_info.value.status_code == 409
    assert _LOCK_TIMEOUT_DETAIL in str(exc_info.value.detail)


def test_classifier_returns_for_a_genuine_connection_error() -> None:
    # Returns (no raise) so the caller's own 503 handling runs unchanged.
    assert raise_lock_timeout_conflict_if(_operational("08006"), "unit.classifier") is None


def test_classifier_returns_for_a_plain_sqlalchemy_error() -> None:
    assert raise_lock_timeout_conflict_if(SQLAlchemyError("boom"), "unit.classifier") is None


# ---------------------------------------------------------------------------
# admin._run_admin_rls_txn (sandbox concurrency / HITL window / flags writers)
# ---------------------------------------------------------------------------


def _admin_principal() -> TenantPrincipal:
    return TenantPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )


def _body_raising(exc: Exception):
    async def _boom() -> None:
        raise exc

    return _boom


async def test_run_admin_rls_txn_maps_lock_timeout_to_409() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await _run_admin_rls_txn(
            MagicMock(),
            _admin_principal(),
            _body_raising(_operational("55P03")),
            detail_admin="admin only",
        )
    assert exc_info.value.status_code == 409
    assert _LOCK_TIMEOUT_DETAIL in str(exc_info.value.detail)


async def test_run_admin_rls_txn_keeps_genuine_db_errors_at_503() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await _run_admin_rls_txn(
            MagicMock(),
            _admin_principal(),
            _body_raising(_operational("08006")),
            detail_admin="admin only",
        )
    assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# product_analytics consent route (real handler, failing session)
# ---------------------------------------------------------------------------


def _principal() -> TenantPrincipal:
    return TenantPrincipal(
        username="tester",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )


def _failing_session(exc: Exception) -> AsyncMock:
    session = AsyncMock()
    configure_rls_preamble(session)

    @asynccontextmanager
    async def _begin() -> AsyncGenerator[None, None]:
        yield

    session.begin = _begin  # type: ignore[assignment]
    session.execute = AsyncMock(side_effect=exc)
    return session


@contextmanager
def _client_overrides(session: AsyncMock) -> Generator[TestClient, None, None]:
    async def override_session() -> AsyncMock:
        yield session

    with (
        patch.object(pa_module, "set_rls_org", new=AsyncMock()),
        patch.object(pa_module, "append_audit_event", new=AsyncMock()),
        patch.object(pa_module, "is_instance_analytics_enabled", new=AsyncMock(return_value=False)),
    ):
        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[pa_module.get_current_tenant_user] = _principal
        yield TestClient(app)
        app.dependency_overrides.clear()


def test_consent_route_maps_lock_timeout_to_409() -> None:
    with _client_overrides(_failing_session(_operational("55P03"))) as client:
        resp = client.post(_CONSENT_URL, json={"action": "accept"})
    assert resp.status_code == 409, resp.text
    assert _LOCK_TIMEOUT_DETAIL in resp.json()["detail"]


def test_consent_route_maps_genuine_db_outage_to_503() -> None:
    """A genuine connection/unavailability error re-raises to
    ``handle_db_errors``, which answers 503 — never the old blanket 500."""
    with _client_overrides(_failing_session(_operational("08006"))) as client:
        resp = client.post(_CONSENT_URL, json={"action": "accept"})
    assert resp.status_code == 503, resp.text


def test_update_level_route_maps_lock_timeout_to_409() -> None:
    """FAR-1832 QA F2: the PUT level route's own SQLAlchemyError arm classifies
    55P03 as a 409 conflict (the same contract as the consent POST)."""
    with _client_overrides(_failing_session(_operational("55P03"))) as client:
        resp = client.put(_PRODUCT_ANALYTICS_LEVEL_URL, json={"level": "all"})
    assert resp.status_code == 409, resp.text
    assert _LOCK_TIMEOUT_DETAIL in resp.json()["detail"]


def test_update_level_route_maps_genuine_db_outage_to_503() -> None:
    """A genuine connection/unavailability error on the PUT level route
    re-raises to ``handle_db_errors``, which answers 503 — never a blanket 500."""
    with _client_overrides(_failing_session(_operational("08006"))) as client:
        resp = client.put(_PRODUCT_ANALYTICS_LEVEL_URL, json={"level": "all"})
    assert resp.status_code == 503, resp.text


async def test_post_consent_direct_call_maps_lock_timeout_to_409() -> None:
    """The handler's own except chain (inside @handle_db_errors) classifies
    55P03 — asserted on a direct coroutine call too."""
    session = _failing_session(_operational("55P03"))
    with (
        patch.object(pa_module, "set_rls_org", new=AsyncMock()),
        pytest.raises(HTTPException) as exc_info,
    ):
        await post_consent.__wrapped__(  # type: ignore[attr-defined]
            pa_module.ConsentRequest(action="accept"), _principal(), session
        )
    assert exc_info.value.status_code == 409


# ---------------------------------------------------------------------------
# FAR-1832 QA F16 sweep guard: every route reaching a bounded-lock patch
# helper must classify 55P03. Prose did not hold — the "sweep is complete"
# claim was falsified twice (F10 missed the toggle + evidence-retention arms;
# F16's own verification found they were still missing).
# ---------------------------------------------------------------------------


# Tokens that identify a route module reaching a bounded-lock org-settings /
# OTel JSON-column patch helper, directly or transitively:
#   - ``update_org_settings_patch`` / ``update_org_json_column_patch``: direct
#   - ``apply_org_settings_patch_or_404``: the route-layer wrapper
#   - ``update_otel_config``: db.crud.observability -> the shared patch
#   - ``set_org_flag``: core.runtime_config.org_flags -> the patch
#   - ``save_policy``: core.evidence_retention -> the patch
_WRITER_TOKENS = (
    "update_org_settings_patch",
    "update_org_json_column_patch",
    "apply_org_settings_patch_or_404",
    "update_otel_config",
    "set_org_flag",
    "save_policy",
)

# The writers known to reach a patch helper today. The guard asserts the
# discovery set is a SUPERSET of this, so a discovery bug (a stale token)
# fails loudly instead of silently passing on an empty set.
_KNOWN_WRITER_MODULES = frozenset(
    {
        "admin.py",
        "admin_email.py",
        "admin_evidence_retention.py",
        "admin_feature_flags.py",
        "admin_orgs.py",
        "costs.py",
        "observability.py",
        "product_analytics.py",
    }
)

_CLASSIFIER_NAME = "raise_lock_timeout_conflict_if"


def _route_sources_reaching_a_patch_helper() -> dict[str, str]:
    """Map route-module name -> source for every module reaching a patch helper.

    Scans ``api/routes/*.py`` (excluding underscore-prefixed shared helpers —
    ``_org_settings`` owns the wrapper but has no request arm to classify) for
    any ``_WRITER_TOKENS`` reference, using a word-boundary match so
    ``set_org_flag`` does not falsely match ``set_org_flag_override``.
    """
    routes_dir = Path(_routes_package.__file__).parent
    discovered: dict[str, str] = {}
    pattern = re.compile("|".join(rf"\b{re.escape(token)}\b" for token in _WRITER_TOKENS))
    for path in sorted(routes_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        source = path.read_text(encoding="utf-8")
        if pattern.search(source):
            discovered[path.name] = source
    return discovered


def test_every_patch_writer_route_classifies_55p03() -> None:
    """FAR-1832 QA F16 sweep guard (the completeness claim was falsified twice).

    A bounded org-settings / OTel JSON-column patch bounds the org row-lock
    wait (``set_mutation_row_lock_timeout``), so SQLSTATE 55P03 is reachable at
    every route arm that fronts one. An arm that lacks
    ``raise_lock_timeout_conflict_if`` collapses the busy-row CONFLICT into a
    generic 503 ("Database temporarily unavailable") — which reads as "retry
    now" and invites a client retry storm against the very lock that is busy.

    The QA F10 sweep claimed to have converted every writer and missed two (the
    feature-flag toggle arm and the evidence-retention route, which carries no
    ``@handle_db_errors`` backstop at all); the independent F16 pass found them
    still missing. This guard makes the invariant STRUCTURAL rather than prose:
    it discovers every route module that reaches a patch helper and fails if any
    of them does not classify 55P03.

    It is non-vacuous by construction — it asserts the discovered set contains
    the known writers, so a stale-token discovery that finds nothing (or too
    little) fails loudly rather than passing.
    """
    discovered = _route_sources_reaching_a_patch_helper()

    assert discovered, (
        "writer discovery found NO route module reaching a patch helper — the guard's "
        "discovery tokens are stale, which is not the same as the sweep being complete"
    )
    missing_known = _KNOWN_WRITER_MODULES - set(discovered)
    assert not missing_known, (
        f"discovery no longer sees known patch-helper writers {sorted(missing_known)} — "
        "fix the discovery tokens, do not weaken the guard"
    )

    unclassified = sorted(name for name, source in discovered.items() if _CLASSIFIER_NAME not in source)
    assert not unclassified, (
        "FAR-1832: these route modules reach a bounded-lock org-settings/OTel patch but never "
        f"call {_CLASSIFIER_NAME}: {unclassified}. Add "
        "raise_lock_timeout_conflict_if(exc, <that route's own log key>) as the FIRST statement "
        "of the arm's except SQLAlchemyError (55P03 must be a 409 conflict, not a 503 outage)."
    )
