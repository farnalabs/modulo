"""Unit tests for the run-scoped credential issuance seam (FAR-1690 slice A1).

Covers:
1. ``IssuedRunCredential`` provenance construction rules — the admitted
   vocabulary and the forbidden ``observed`` attestation kind.
2. The credential never renders its secret in ``repr``.
3. ``ModuloCredentialIssuer.issue_for_run`` — fail-open paths, the minted
   ``modulo_minted``/``attested`` provenance, and account resolution.
4. ``revoke_for_run`` / ``describe`` / ``default_credential_issuer``.
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.credential_issuer import (
    ISSUANCE_PROVENANCE_BROKER_ISSUED,
    ISSUANCE_PROVENANCE_MODULO_MINTED,
    IssuedRunCredential,
    ModuloCredentialIssuer,
    _coerce_uuid,
    default_credential_issuer,
)
from tests.unit.api.mock_session import configure_rls_preamble

_ORG_ID = "11111111-1111-1111-1111-111111111111"
_RUN_ID = "22222222-2222-2222-2222-222222222222"
_ACCOUNT_ID = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
_FAKE_KEY = "mk_fake_run_key"


def _session_factory(account_rows: list[Any] | None = None):
    """Session factory whose ``execute`` yields the given account rows in order."""
    session = configure_rls_preamble(AsyncMock())
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=session)
    rows = account_rows if account_rows is not None else [(str(_ACCOUNT_ID),)]
    results = []
    for row in rows:
        result = MagicMock()
        result.fetchone.return_value = row
        results.append(result)
    session.execute = AsyncMock(side_effect=results)
    return session, MagicMock(return_value=session)


def _issued(**overrides: Any) -> IssuedRunCredential:
    base: dict[str, Any] = {
        "run_id": uuid.UUID(_RUN_ID),
        "node_id": "n1",
        "secret": "top-secret-value",
    }
    base.update(overrides)
    return IssuedRunCredential(**base)


class TestIssuedRunCredentialProvenance:
    def test_defaults_to_modulo_minted_attested(self) -> None:
        cred = _issued()
        assert cred.provenance == ISSUANCE_PROVENANCE_MODULO_MINTED
        assert cred.provenance_kind == "attested"

    def test_accepts_broker_issued_provenance(self) -> None:
        cred = _issued(provenance=ISSUANCE_PROVENANCE_BROKER_ISSUED, provenance_kind="declared")
        assert cred.provenance == ISSUANCE_PROVENANCE_BROKER_ISSUED

    def test_rejects_unknown_provenance(self) -> None:
        with pytest.raises(ValueError, match="invalid issuance provenance"):
            _issued(provenance="something_else")

    def test_rejects_observed_attestation_kind(self) -> None:
        with pytest.raises(ValueError, match="never be 'observed'"):
            _issued(provenance_kind="observed")

    def test_secret_is_absent_from_repr(self) -> None:
        cred = _issued()
        assert "top-secret-value" not in repr(cred)
        assert "run_id" in repr(cred)


class TestCoerceUuid:
    def test_uuid_instance_is_returned_unchanged(self) -> None:
        value = uuid.UUID(_RUN_ID)
        assert _coerce_uuid(value) is value

    def test_none_and_empty_string_return_none(self) -> None:
        assert _coerce_uuid(None) is None
        assert _coerce_uuid("") is None

    def test_valid_string_is_parsed(self) -> None:
        assert _coerce_uuid(_ORG_ID) == uuid.UUID(_ORG_ID)

    def test_invalid_string_returns_none(self) -> None:
        assert _coerce_uuid("not-a-uuid") is None


class TestModuloCredentialIssuer:
    async def test_issue_without_session_factory_returns_none(self) -> None:
        issuer = ModuloCredentialIssuer()
        issued = await issuer.issue_for_run(
            session_factory=None, org_id=_ORG_ID, run_id=_RUN_ID, node_id="n1", sandbox_timeout=300
        )
        assert issued is None

    async def test_issue_with_unparseable_org_returns_none(self) -> None:
        _session, factory = _session_factory()
        issuer = ModuloCredentialIssuer()
        issued = await issuer.issue_for_run(
            session_factory=factory, org_id="not-a-uuid", run_id=_RUN_ID, node_id="n1", sandbox_timeout=300
        )
        assert issued is None

    async def test_issue_with_unparseable_run_returns_none(self) -> None:
        _session, factory = _session_factory()
        issuer = ModuloCredentialIssuer()
        issued = await issuer.issue_for_run(
            session_factory=factory, org_id=_ORG_ID, run_id="not-a-uuid", node_id="n1", sandbox_timeout=300
        )
        assert issued is None

    async def test_issue_success_stamps_modulo_minted(self) -> None:
        session, factory = _session_factory()
        captured: dict[str, Any] = {}

        async def _mint(sess, *, org_id, run_id, node_id, account_id, ttl_seconds):
            captured["account_id"] = account_id
            captured["ttl_seconds"] = ttl_seconds
            return MagicMock(), _FAKE_KEY

        with (
            patch("modulo.auth.api_key.mint_run_api_key", new=_mint),
            patch("modulo.db.crud.run.get_run_api_key_ttl_seconds", new=AsyncMock(return_value=1800)),
        ):
            issued = await ModuloCredentialIssuer().issue_for_run(
                session_factory=factory, org_id=_ORG_ID, run_id=_RUN_ID, node_id="n1", sandbox_timeout=300
            )

        assert issued is not None
        assert issued.secret == _FAKE_KEY
        assert issued.provenance == ISSUANCE_PROVENANCE_MODULO_MINTED
        assert issued.provenance_kind == "attested"
        assert issued.issuer == "modulo"
        assert str(issued.run_id) == _RUN_ID
        assert issued.node_id == "n1"
        assert captured["account_id"] == _ACCOUNT_ID
        assert session.execute.await_count == 1

    async def test_issue_falls_back_to_first_active_admin(self) -> None:
        admin_id = uuid.UUID("bbbbbbbb-cccc-dddd-eeee-ffff00000001")
        _session, factory = _session_factory([None, (str(admin_id),)])
        captured: dict[str, Any] = {}

        async def _mint(sess, *, org_id, run_id, node_id, account_id, ttl_seconds):
            captured["account_id"] = account_id
            return MagicMock(), _FAKE_KEY

        with (
            patch("modulo.auth.api_key.mint_run_api_key", new=_mint),
            patch("modulo.db.crud.run.get_run_api_key_ttl_seconds", new=AsyncMock(return_value=1800)),
        ):
            issued = await ModuloCredentialIssuer().issue_for_run(
                session_factory=factory, org_id=_ORG_ID, run_id=_RUN_ID, node_id="n1", sandbox_timeout=300
            )

        assert issued is not None
        assert captured["account_id"] == admin_id

    async def test_issue_returns_none_when_mint_returns_none(self) -> None:
        _session, factory = _session_factory()
        with (
            patch("modulo.auth.api_key.mint_run_api_key", new=AsyncMock(return_value=None)),
            patch("modulo.db.crud.run.get_run_api_key_ttl_seconds", new=AsyncMock(return_value=1800)),
        ):
            issued = await ModuloCredentialIssuer().issue_for_run(
                session_factory=factory, org_id=_ORG_ID, run_id=_RUN_ID, node_id="n1", sandbox_timeout=300
            )
        assert issued is None

    async def test_issue_failure_is_swallowed(self) -> None:
        _session, factory = _session_factory()
        with (
            patch("modulo.auth.api_key.mint_run_api_key", new=AsyncMock(side_effect=RuntimeError("boom"))),
            patch("modulo.db.crud.run.get_run_api_key_ttl_seconds", new=AsyncMock(return_value=1800)),
        ):
            issued = await ModuloCredentialIssuer().issue_for_run(
                session_factory=factory, org_id=_ORG_ID, run_id=_RUN_ID, node_id="n1", sandbox_timeout=300
            )
        assert issued is None

    async def test_revoke_for_run_delegates_to_revoke_run_api_key(self) -> None:
        run_id = uuid.UUID(_RUN_ID)
        org_id = uuid.UUID(_ORG_ID)
        with patch("modulo.auth.api_key.revoke_run_api_key", new=AsyncMock(return_value=3)) as revoke:
            count = await ModuloCredentialIssuer().revoke_for_run(MagicMock(), run_id=run_id, org_id=org_id)
        assert count == 3
        assert revoke.await_args.kwargs["run_id"] == run_id

    def test_describe_reports_provenance_without_secret(self) -> None:
        described = ModuloCredentialIssuer().describe()
        assert described["issuer"] == "modulo"
        assert described["provenance"] == ISSUANCE_PROVENANCE_MODULO_MINTED
        assert described["provenance_kind"] == "attested"

    def test_default_issuer_is_modulo_minted(self) -> None:
        issuer = default_credential_issuer()
        assert issuer.name == "modulo"
        assert issuer.provenance == ISSUANCE_PROVENANCE_MODULO_MINTED
