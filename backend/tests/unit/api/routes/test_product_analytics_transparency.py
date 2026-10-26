"""Unit tests for the product-analytics transparency + instance-switch routes.

The endpoints report the instance's REAL product-analytics posture. These tests
pin that every field is sourced from the state a feature actually writes:

* ``instance_enabled`` / ``instance_switch_source`` / ``licence_requires_analytics``
  run the REAL shared resolver (``consent.resolve_instance_analytics_switch``)
  with only its ``get_config`` DB seam and the licence read patched — so the
  fail-closed coercion, the env fallback and the licence-derived default the
  dump gate inherits are exercised here too (a stored ``"false"`` must be OFF).
* ``last_successful_dump_at`` / ``dump_count_total`` read the exact keys the
  metrics dump writes (``DUMP_WATERMARK_KEY`` / ``DUMP_COUNT_KEY``).
* ``consent_level`` is the caller's organisation real consent level
  (``org.settings_json["product_analytics"]["level"]``) — the PREFERRED source.
  When the caller org cannot be resolved the endpoint reports an INSTANCE-level
  posture (``all`` if any active org has opted in, else ``off``) instead of a
  hardcoded ``off`` (FAR-1635).
* ``PUT /instance-switch`` writes/clears the stored operator value, records the
  change in the org-independent system audit ledger, and returns the resulting
  effective state (an explicit operator value always wins; clearing restores
  the licence-derived default).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.unit.api.mock_session import configure_mock_session, configure_rls_preamble

from modulo.api.dependencies import get_db_session
from modulo.api.routes import product_analytics_transparency as pat_module
from modulo.api.routes.product_analytics_transparency import (
    TransparencyResponse,
    _coerce_last_dump,
    _resolve_org,
)
from modulo.api.routes.product_analytics_transparency import router as transparency_router
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.audit_coverage import audit_session
from modulo.core.product_analytics.constants import (
    DUMP_COUNT_KEY,
    DUMP_WATERMARK_KEY,
    INSTANCE_SWITCH_KEY,
    LEVEL_ALL,
    LEVEL_OFF,
    PARTNER_LICENSE_CLAIM,
    SWITCH_SOURCE_DEFAULT,
    SWITCH_SOURCE_ENV,
    SWITCH_SOURCE_LICENSE,
    SWITCH_SOURCE_OPERATOR,
)

app = FastAPI()
app.include_router(transparency_router)

_URL = "/api/v1/product-analytics/transparency"

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")

_SYSTEM_ADMIN = AuthenticatedPrincipal(
    username="ops@test",
    organisation_id=_ORG_ID,
    account_id=_USER_ID,
    org_role="admin",
    is_system_admin=True,
)

_NO_ORG_ADMIN = AuthenticatedPrincipal(
    username="ops@test",
    organisation_id=None,
    account_id=_USER_ID,
    org_role=None,
    is_system_admin=True,
)

_NON_SYSTEM_ADMIN = AuthenticatedPrincipal(
    username="user@test",
    organisation_id=_ORG_ID,
    account_id=_USER_ID,
    org_role="admin",
    is_system_admin=False,
)

# Sentinel distinguishing "no org row" from an org whose settings are None.
_UNSET = object()

_ORG_ALL = {"product_analytics": {"level": LEVEL_ALL}}
_ORG_OFF = {"product_analytics": {"level": LEVEL_OFF}}

_SWITCH_URL = "/api/v1/product-analytics/instance-switch"


def _licence(*, claim: bool = True) -> SimpleNamespace:
    """A minimal LicenseData-like object carrying a signed ``raw_payload``."""
    return SimpleNamespace(raw_payload={PARTNER_LICENSE_CLAIM: claim})


@pytest.fixture(autouse=True)
def _default_no_licence(monkeypatch: pytest.MonkeyPatch) -> None:
    """No installed licence by default; licence tests override this seam."""
    monkeypatch.setattr("modulo.core.product_analytics.consent.get_license", lambda: None)


@pytest.fixture(autouse=True)
def _stub_audit_session() -> AsyncGenerator[None, None]:
    """The ``audited()`` dependency writes on a fresh session (real engine in prod).

    No database exists in the unit tier, so stub that seam; the dependency (and
    its request/principal resolution) still runs around the PUT.
    """

    async def _override() -> AsyncGenerator[AsyncMock, None]:
        session = configure_mock_session(AsyncMock(), allow_empty_execute=True)
        yield session

    app.dependency_overrides[audit_session] = _override
    yield
    app.dependency_overrides.pop(audit_session, None)


def _config(value: object) -> MagicMock:
    config = MagicMock()
    config.value = value
    return config


def _org_result(org_settings: object) -> MagicMock:
    """A SELECT result carrying the caller-org lookup (``scalar_one_or_none``)."""
    result = MagicMock()
    if org_settings is _UNSET:
        result.scalar_one_or_none = MagicMock(return_value=None)
    else:
        org = MagicMock()
        org.settings_json = org_settings
        result.scalar_one_or_none = MagicMock(return_value=org)
    return result


def _instances_result(entries: list[object]) -> MagicMock:
    """A SELECT result carrying active orgs (``scalars()``) for the fallback.

    A plain-string entry is a consent level and is wrapped as the org's
    ``settings_json`` (``{"product_analytics": {"level": ...}}``); ``None`` and
    any non-string entry (a dict/list) are used verbatim, so a malformed
    ``settings_json`` shape can be modelled too.
    """
    orgs = []
    for entry in entries:
        org = MagicMock()
        org.settings_json = {"product_analytics": {"level": entry}} if isinstance(entry, str) else entry
        orgs.append(org)
    result = MagicMock()
    result.scalars = MagicMock(return_value=iter(orgs))
    return result


def _make_session(
    org_settings: object = _UNSET,
    instance_levels: list[object] | None = None,
    *,
    skip_org_lookup: bool = False,
) -> AsyncMock:
    """Build a session mock resolving the caller org, then the instance aggregate.

    ``org_settings`` is the returned caller-org's ``settings_json``; ``_UNSET``
    means no org row is found (``scalar_one_or_none()`` -> ``None``). The second
    result models the instance-aggregate read (``_instance_consent_level``),
    built from ``instance_levels`` (one entry per active org). The endpoint only
    issues the second SELECT when the caller org is unresolved.

    ``skip_org_lookup`` drops the org-lookup result: a principal with no
    ``organisation_id`` short-circuits ``_resolve_org`` without a query, so the
    instance aggregate becomes the FIRST execute.
    """
    session = AsyncMock()
    configure_rls_preamble(session)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    results: list[MagicMock] = []
    if not skip_org_lookup:
        results.append(_org_result(org_settings))
    results.append(_instances_result(list(instance_levels or [])))
    session.execute = AsyncMock(side_effect=results)
    return session


def _write_session(org_settings: object = _ORG_ALL) -> AsyncMock:
    """Session double for the PUT: every statement succeeds, the org resolves.

    The CRUD writers and the system-audit append are patched in the write tests,
    so ``execute`` only has to serve the ``_resolve_org`` lookup.
    """
    session = AsyncMock()
    configure_rls_preamble(session)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.execute = AsyncMock(return_value=_org_result(org_settings))
    return session


def _client(
    principal: AuthenticatedPrincipal = _SYSTEM_ADMIN,
    session: AsyncMock | None = None,
) -> TestClient:
    app.dependency_overrides[get_db_session] = lambda: session if session is not None else _make_session()
    app.dependency_overrides[get_current_user] = lambda: principal
    return TestClient(app)


def _restore_overrides() -> None:
    app.dependency_overrides.clear()


def _consent_get_config(values: dict[str, object]):
    """A ``consent.get_config`` replacement keyed by the instance-switch setting."""

    async def _get(session: object, key: str) -> MagicMock | None:
        if key in values:
            return _config(values[key])
        return None

    return _get


def _transparency_get_config(values: dict[str, object]):
    """A route ``get_config`` replacement keyed by the dump watermark / count."""

    async def _get(session: object, key: str) -> MagicMock | None:
        if key in values:
            return _config(values[key])
        return None

    return _get


def _request(client: TestClient) -> dict[str, object]:
    resp = client.get(_URL)
    assert resp.status_code == 200
    return dict(resp.json())


def _stale_timestamp_ago(days: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _patch_sources(
    *,
    consent_values: dict[str, object] | None = None,
    transparency_values: dict[str, object] | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
):
    """Patch both DB seams and return the transparency ``AsyncMock``."""
    if monkeypatch is not None:
        monkeypatch.delenv("MODULO_PRODUCT_ANALYTICS_ENABLED", raising=False)
    return patch.object(
        pat_module,
        "get_config",
        new=AsyncMock(side_effect=_transparency_get_config(transparency_values or {})),
    ), patch(
        "modulo.core.product_analytics.consent.get_config",
        new=AsyncMock(side_effect=_consent_get_config(consent_values or {})),
    )


# ---------------------------------------------------------------------------
# Permission gate
# ---------------------------------------------------------------------------


class TestPermissionGate:
    def test_non_system_admin_gets_403(self) -> None:
        client = _client(_NON_SYSTEM_ADMIN)
        resp = client.get(_URL)
        assert resp.status_code == 403
        _restore_overrides()

    def test_system_admin_gets_200(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _client()
        sources = _patch_sources(monkeypatch=monkeypatch)
        with sources[0], sources[1]:
            resp = client.get(_URL)
        assert resp.status_code == 200
        _restore_overrides()


# ---------------------------------------------------------------------------
# Defaults (no rows stored)
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_all_absent_returns_zeros_and_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _client()
        sources = _patch_sources(monkeypatch=monkeypatch)
        with sources[0], sources[1]:
            body = _request(client)
        assert body["last_successful_dump_at"] is None
        assert body["dump_count_total"] == 0
        assert body["consent_level"] == LEVEL_OFF
        assert body["instance_enabled"] is False
        # No operator setting and no licence claim: the default source is OFF.
        assert body["instance_switch_source"] == SWITCH_SOURCE_DEFAULT
        assert body["licence_requires_analytics"] is False
        assert body["egress_allowed"] is False
        assert body["warning"] is None
        _restore_overrides()

    def test_response_shape_matches_transparency_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _client()
        sources = _patch_sources(monkeypatch=monkeypatch)
        with sources[0], sources[1]:
            body = _request(client)
        assert set(body.keys()) == set(TransparencyResponse().model_dump().keys())
        _restore_overrides()


# ---------------------------------------------------------------------------
# Real instance switch sources (operator -> licence default)
# ---------------------------------------------------------------------------


class TestRealInstanceSwitch:
    def test_stored_false_string_reads_fail_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression: a stored string ``"false"`` must NOT read as enabled."""
        client = _client()
        sources = _patch_sources(consent_values={INSTANCE_SWITCH_KEY: "false"}, monkeypatch=monkeypatch)
        with sources[0], sources[1]:
            body = _request(client)
        assert body["instance_enabled"] is False
        assert body["instance_switch_source"] == SWITCH_SOURCE_OPERATOR
        _restore_overrides()

    @pytest.mark.parametrize("stored", [True, "1", "true", "yes"])
    def test_enabling_values_read_enabled(self, stored: object, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _client()
        sources = _patch_sources(consent_values={INSTANCE_SWITCH_KEY: stored}, monkeypatch=monkeypatch)
        with sources[0], sources[1]:
            body = _request(client)
        assert body["instance_enabled"] is True
        assert body["instance_switch_source"] == SWITCH_SOURCE_OPERATOR
        _restore_overrides()

    def test_env_fallback_when_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _client()
        sources = _patch_sources(monkeypatch=monkeypatch)
        monkeypatch.setenv("MODULO_PRODUCT_ANALYTICS_ENABLED", "true")
        with sources[0], sources[1]:
            body = _request(client)
        assert body["instance_enabled"] is True
        assert body["instance_switch_source"] == SWITCH_SOURCE_ENV
        _restore_overrides()

    def test_licence_claim_sets_the_default_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "modulo.core.product_analytics.consent.get_license",
            lambda: _licence(claim=True),
        )
        client = _client()
        sources = _patch_sources(monkeypatch=monkeypatch)
        with sources[0], sources[1]:
            body = _request(client)
        assert body["instance_enabled"] is True
        assert body["instance_switch_source"] == SWITCH_SOURCE_LICENSE
        assert body["licence_requires_analytics"] is True
        _restore_overrides()

    def test_operator_off_vetoes_the_licence_claim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "modulo.core.product_analytics.consent.get_license",
            lambda: _licence(claim=True),
        )
        client = _client()
        sources = _patch_sources(consent_values={INSTANCE_SWITCH_KEY: False}, monkeypatch=monkeypatch)
        with sources[0], sources[1]:
            body = _request(client)
        assert body["instance_enabled"] is False
        assert body["instance_switch_source"] == SWITCH_SOURCE_OPERATOR
        assert body["licence_requires_analytics"] is True
        _restore_overrides()


# ---------------------------------------------------------------------------
# The system-admin write path (PUT /instance-switch)
# ---------------------------------------------------------------------------


class TestInstanceSwitchWrite:
    def test_system_admin_sets_explicit_value_and_is_audited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _client(session=_write_session())
        stored: dict[str, object] = {}

        async def _fake_update(_session: object, key: str, value: object, updated_by: object = None) -> MagicMock:
            stored[key] = value
            return MagicMock()

        sources = _patch_sources(monkeypatch=monkeypatch)
        with (
            sources[0],
            patch(
                "modulo.core.product_analytics.consent.get_config",
                new=AsyncMock(side_effect=_consent_get_config(stored)),
            ),
            patch.object(pat_module, "update_config", new=AsyncMock(side_effect=_fake_update)),
            patch.object(pat_module, "append_system_audit_event", new=AsyncMock()) as audit,
        ):
            resp = client.put(_SWITCH_URL, json={"enabled": True})

        assert resp.status_code == 200
        body = resp.json()
        assert body["instance_enabled"] is True
        assert body["instance_switch_source"] == SWITCH_SOURCE_OPERATOR
        audit.assert_awaited_once()
        payload = audit.await_args.kwargs["payload_json"]
        assert payload["action"] == "set"
        assert payload["operator_value"] is True
        assert payload["effective_enabled"] is True
        _restore_overrides()

    def test_clearing_restores_the_licence_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "modulo.core.product_analytics.consent.get_license",
            lambda: _licence(claim=True),
        )
        client = _client(session=_write_session())
        stored: dict[str, object] = {INSTANCE_SWITCH_KEY: False}

        async def _fake_delete(_session: object, key: str) -> bool:
            return stored.pop(key, None) is not None

        sources = _patch_sources(monkeypatch=monkeypatch)
        with (
            sources[0],
            patch(
                "modulo.core.product_analytics.consent.get_config",
                new=AsyncMock(side_effect=_consent_get_config(stored)),
            ),
            patch.object(pat_module, "delete_config", new=AsyncMock(side_effect=_fake_delete)),
            patch.object(pat_module, "append_system_audit_event", new=AsyncMock()) as audit,
        ):
            resp = client.put(_SWITCH_URL, json={"enabled": None})

        assert resp.status_code == 200
        body = resp.json()
        assert body["instance_enabled"] is True
        assert body["instance_switch_source"] == SWITCH_SOURCE_LICENSE
        assert audit.await_args.kwargs["payload_json"]["action"] == "cleared"
        _restore_overrides()

    def test_non_system_admin_gets_403(self) -> None:
        client = _client(_NON_SYSTEM_ADMIN, session=_write_session())
        resp = client.put(_SWITCH_URL, json={"enabled": True})
        assert resp.status_code == 403
        _restore_overrides()


# ---------------------------------------------------------------------------
# Per-org consent level (the PREFERRED source)
# ---------------------------------------------------------------------------


class TestRealConsentLevel:
    def test_org_level_all_is_reflected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _client(session=_make_session(_ORG_ALL))
        sources = _patch_sources(consent_values={INSTANCE_SWITCH_KEY: True}, monkeypatch=monkeypatch)
        with sources[0], sources[1]:
            body = _request(client)
        assert body["consent_level"] == LEVEL_ALL
        # Egress is opt-in on BOTH axes; instance switch on + org "all" -> allowed.
        assert body["egress_allowed"] is True
        _restore_overrides()

    def test_org_level_off_is_reflected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = _make_session(_ORG_OFF)
        client = _client(session=session)
        sources = _patch_sources(consent_values={INSTANCE_SWITCH_KEY: True}, monkeypatch=monkeypatch)
        with sources[0], sources[1]:
            body = _request(client)
        assert body["consent_level"] == LEVEL_OFF
        assert body["egress_allowed"] is False
        # A resolved caller org is the sole source — no instance aggregate read.
        assert session.execute.await_count == 1
        _restore_overrides()

    def test_per_org_off_wins_over_instance_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Per-org is primary: the caller's own ``off`` must not be overridden."""
        session = _make_session(_ORG_OFF, instance_levels=[LEVEL_ALL])
        client = _client(session=session)
        sources = _patch_sources(consent_values={INSTANCE_SWITCH_KEY: True}, monkeypatch=monkeypatch)
        with sources[0], sources[1]:
            body = _request(client)
        assert body["consent_level"] == LEVEL_OFF
        assert body["egress_allowed"] is False
        assert session.execute.await_count == 1
        _restore_overrides()

    def test_org_without_block_defaults_to_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _client(session=_make_session({"other": True}))
        sources = _patch_sources(consent_values={INSTANCE_SWITCH_KEY: True}, monkeypatch=monkeypatch)
        with sources[0], sources[1]:
            body = _request(client)
        assert body["consent_level"] == LEVEL_OFF
        _restore_overrides()

    def test_org_with_malformed_settings_defaults_to_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-dict ``settings_json`` must not crash the per-org read (fail-closed)."""
        session = _make_session(["not", "a", "dict"])
        client = _client(session=session)
        sources = _patch_sources(consent_values={INSTANCE_SWITCH_KEY: True}, monkeypatch=monkeypatch)
        with sources[0], sources[1]:
            body = _request(client)
        assert body["consent_level"] == LEVEL_OFF
        assert body["egress_allowed"] is False
        assert session.execute.await_count == 1
        _restore_overrides()

    def test_resolved_org_without_settings_ignores_instance_aggregate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A resolved org with null settings is per-org ``off``, NOT the instance posture."""
        session = _make_session(None, instance_levels=[LEVEL_ALL])
        client = _client(session=session)
        sources = _patch_sources(consent_values={INSTANCE_SWITCH_KEY: True}, monkeypatch=monkeypatch)
        with sources[0], sources[1]:
            body = _request(client)
        assert body["consent_level"] == LEVEL_OFF
        assert session.execute.await_count == 1
        _restore_overrides()


# ---------------------------------------------------------------------------
# Instance fallback when the caller org cannot be resolved (FAR-1635)
# ---------------------------------------------------------------------------


class TestInstanceConsentFallback:
    def test_missing_org_falls_back_to_instance_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _client(session=_make_session(instance_levels=[LEVEL_OFF, LEVEL_ALL]))
        sources = _patch_sources(consent_values={INSTANCE_SWITCH_KEY: True}, monkeypatch=monkeypatch)
        with sources[0], sources[1]:
            body = _request(client)
        assert body["consent_level"] == LEVEL_ALL
        # Instance switch on + instance aggregate all -> egress allowed.
        assert body["egress_allowed"] is True
        _restore_overrides()

    def test_missing_org_falls_back_to_instance_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _client(session=_make_session(instance_levels=[LEVEL_OFF]))
        sources = _patch_sources(consent_values={INSTANCE_SWITCH_KEY: True}, monkeypatch=monkeypatch)
        with sources[0], sources[1]:
            body = _request(client)
        assert body["consent_level"] == LEVEL_OFF
        assert body["egress_allowed"] is False
        _restore_overrides()

    def test_no_active_org_falls_back_to_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _client(session=_make_session())  # no org row, no active consenting org
        sources = _patch_sources(consent_values={INSTANCE_SWITCH_KEY: True}, monkeypatch=monkeypatch)
        with sources[0], sources[1]:
            body = _request(client)
        assert body["consent_level"] == LEVEL_OFF
        _restore_overrides()

    def test_malformed_instance_settings_fall_back_to_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-dict ``settings_json`` row must not 500 the aggregate (fail-closed)."""
        client = _client(session=_make_session(instance_levels=[["not", "a", "dict"], LEVEL_OFF]))
        sources = _patch_sources(consent_values={INSTANCE_SWITCH_KEY: True}, monkeypatch=monkeypatch)
        with sources[0], sources[1]:
            body = _request(client)
        assert body["consent_level"] == LEVEL_OFF
        assert body["egress_allowed"] is False
        _restore_overrides()

    def test_malformed_row_does_not_mask_a_consenting_org(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A malformed row must be skipped, not abort the scan before a consenting org."""
        client = _client(session=_make_session(instance_levels=[["not", "a", "dict"], LEVEL_ALL]))
        sources = _patch_sources(consent_values={INSTANCE_SWITCH_KEY: True}, monkeypatch=monkeypatch)
        with sources[0], sources[1]:
            body = _request(client)
        assert body["consent_level"] == LEVEL_ALL
        _restore_overrides()

    def test_org_less_principal_falls_back_to_instance_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _client(
            principal=_NO_ORG_ADMIN,
            session=_make_session(instance_levels=[LEVEL_ALL], skip_org_lookup=True),
        )
        sources = _patch_sources(consent_values={INSTANCE_SWITCH_KEY: True}, monkeypatch=monkeypatch)
        with sources[0], sources[1]:
            body = _request(client)
        assert body["consent_level"] == LEVEL_ALL
        _restore_overrides()

    def test_org_less_principal_falls_back_to_instance_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _client(
            principal=_NO_ORG_ADMIN,
            session=_make_session(instance_levels=[LEVEL_OFF], skip_org_lookup=True),
        )
        sources = _patch_sources(consent_values={INSTANCE_SWITCH_KEY: True}, monkeypatch=monkeypatch)
        with sources[0], sources[1]:
            body = _request(client)
        assert body["consent_level"] == LEVEL_OFF
        _restore_overrides()


# ---------------------------------------------------------------------------
# Real watermark / count sources
# ---------------------------------------------------------------------------


class TestRealDumpSources:
    def test_watermark_key_drives_last_dump(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stamp = _stale_timestamp_ago(1)
        client = _client(session=_make_session(_ORG_ALL))
        sources = _patch_sources(
            transparency_values={DUMP_WATERMARK_KEY: stamp},
            monkeypatch=monkeypatch,
        )
        with sources[0], sources[1]:
            body = _request(client)
        assert body["last_successful_dump_at"] == stamp
        _restore_overrides()

    def test_route_reads_the_dump_written_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _client(session=_make_session(_ORG_ALL))
        sources = _patch_sources(
            transparency_values={
                DUMP_WATERMARK_KEY: "2026-08-15",
                DUMP_COUNT_KEY: 7,
            },
            monkeypatch=monkeypatch,
        )
        with sources[0] as get_config, sources[1]:
            body = _request(client)
        keys = [call.args[1] for call in get_config.await_args_list]
        assert DUMP_WATERMARK_KEY in keys
        assert DUMP_COUNT_KEY in keys
        assert body["dump_count_total"] == 7
        _restore_overrides()

    @pytest.mark.parametrize(
        ("dump_count", "expected"),
        [
            (None, 0),
            ("0", 0),
            ("42", 42),
            (0, 0),
            ("not-a-number", 0),
            (-5, 0),
        ],
    )
    def test_dump_count_coerces_to_non_negative_int(
        self,
        dump_count: object,
        expected: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = _client(session=_make_session(_ORG_ALL))
        transparency_values = {} if dump_count is None else {DUMP_COUNT_KEY: dump_count}
        sources = _patch_sources(transparency_values=transparency_values, monkeypatch=monkeypatch)
        with sources[0], sources[1]:
            body = _request(client)
        assert body["dump_count_total"] == expected
        _restore_overrides()

    def test_non_string_watermark_is_stringified(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A stored non-string watermark is stringified, not dropped to None."""
        client = _client(session=_make_session(_ORG_ALL))
        sources = _patch_sources(
            transparency_values={DUMP_WATERMARK_KEY: 12345},
            monkeypatch=monkeypatch,
        )
        with sources[0], sources[1]:
            body = _request(client)
        assert body["last_successful_dump_at"] == "12345"
        # 12345 is not an ISO timestamp, so the staleness check fails silent.
        assert body["warning"] is None
        _restore_overrides()


# ---------------------------------------------------------------------------
# _coerce_last_dump (the watermark coercion)
# ---------------------------------------------------------------------------


class TestCoerceLastDump:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("2026-08-15", "2026-08-15"),  # a stored string passes through
            (12345, "12345"),  # any other non-None scalar is stringified
            (None, None),  # a missing value yields None
        ],
    )
    def test_coerces_stored_watermark(self, value: object, expected: object) -> None:
        assert _coerce_last_dump(value) == expected


# ---------------------------------------------------------------------------
# Stale-dump warning logic (the derived field)
# ---------------------------------------------------------------------------


class TestStaleWarning:
    @pytest.mark.parametrize(
        ("days_ago", "consent_level", "expected_warning"),
        [
            # Inside the 3-day threshold — never warns.
            (2, LEVEL_ALL, None),
            # Fresh dump but consent not 'all' — warning suppressed by consent.
            (2, LEVEL_OFF, None),
            # Just past the threshold with opt-in consent — warns.
            (4, LEVEL_ALL, "not_reaching_farnalabs"),
            # Stale dump but consent opt-out — not actionable, no warning.
            (4, LEVEL_OFF, None),
        ],
    )
    def test_warning_boundary(
        self,
        days_ago: float,
        consent_level: str,
        expected_warning: str | None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = _client(session=_make_session({"product_analytics": {"level": consent_level}}))
        sources = _patch_sources(
            transparency_values={DUMP_WATERMARK_KEY: _stale_timestamp_ago(days_ago)},
            monkeypatch=monkeypatch,
        )
        with sources[0], sources[1]:
            body = _request(client)
        assert body["warning"] == expected_warning
        _restore_overrides()

    def test_no_last_dump_never_warns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _client(session=_make_session(_ORG_ALL))
        sources = _patch_sources(monkeypatch=monkeypatch)
        with sources[0], sources[1]:
            body = _request(client)
        assert body["last_successful_dump_at"] is None
        assert body["warning"] is None
        _restore_overrides()

    def test_naive_timestamp_is_treated_as_utc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A naive ``last_dump_at`` (no tzinfo) is assumed UTC for age math."""
        naive = (datetime.now(UTC).replace(tzinfo=None) - timedelta(days=4)).isoformat()
        client = _client(session=_make_session(_ORG_ALL))
        sources = _patch_sources(
            transparency_values={DUMP_WATERMARK_KEY: naive},
            monkeypatch=monkeypatch,
        )
        with sources[0], sources[1]:
            body = _request(client)
        assert body["warning"] == "not_reaching_farnalabs"
        _restore_overrides()

    def test_malformed_timestamp_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _client(session=_make_session(_ORG_ALL))
        sources = _patch_sources(
            transparency_values={DUMP_WATERMARK_KEY: "not-a-timestamp"},
            monkeypatch=monkeypatch,
        )
        with sources[0], sources[1]:
            body = _request(client)
        assert body["last_successful_dump_at"] == "not-a-timestamp"
        assert body["warning"] is None
        _restore_overrides()


# ---------------------------------------------------------------------------
# _resolve_org (the org DB seam)
# ---------------------------------------------------------------------------


class TestResolveOrg:
    @pytest.mark.asyncio
    async def test_returns_org_for_existing_id(self) -> None:
        session = _make_session(_ORG_ALL)
        org = await _resolve_org(session, _ORG_ID)
        assert org is not None
        assert org.settings_json == _ORG_ALL

    @pytest.mark.asyncio
    async def test_returns_org_with_null_settings(self) -> None:
        """A resolved org with no settings_json is still a resolved org."""
        session = _make_session(None)
        org = await _resolve_org(session, _ORG_ID)
        assert org is not None
        assert org.settings_json is None

    @pytest.mark.asyncio
    async def test_returns_none_when_org_missing(self) -> None:
        session = _make_session()
        assert await _resolve_org(session, _ORG_ID) is None

    @pytest.mark.asyncio
    async def test_none_org_id_skips_the_query(self) -> None:
        session = _make_session(_ORG_ALL)
        assert await _resolve_org(session, None) is None
        session.execute.assert_not_awaited()
