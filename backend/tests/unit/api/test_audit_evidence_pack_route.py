"""Unit tests for the audit evidence-pack + verification-key routes (FAR-1697).

Hermetic, DB-free: the session factory and the chain streamer are patched out,
and the signing key is supplied through a patched Settings namespace — so the
route is exercised end-to-end (stream -> sign -> publish) and the resulting
pack is then verified offline by an independent verifier.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

import modulo.core.evidence_pack.build as evidence_build
import modulo.core.evidence_pack.signing as evidence_signing
from modulo.api.routes import audit as route
from modulo.auth.jwt import TenantPrincipal
from modulo.core.evidence_pack import EvidenceSigningError, verify_evidence_pack
from modulo.registry.crypto import generate_keypair

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
_ACCOUNT = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


def _principal() -> TenantPrincipal:
    return TenantPrincipal(username="evidence-user", organisation_id=_ORG, account_id=_ACCOUNT, org_role="admin")


def _event(event_id: str = "evt-1") -> dict[str, Any]:
    return {
        "id": event_id,
        "event_type": "event.a",
        "actor_user_id": None,
        "resource_type": None,
        "resource_id": None,
        "payload_json": {},
        "request_id": None,
        "previous_hash": None,
        "created_at": "2026-01-01T00:00:00+00:00",
    }


async def _stream(*items: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    for item in items:
        yield item


def _settings_namespace(key_pem: str) -> SimpleNamespace:
    return SimpleNamespace(modulo_evidence_signing_key=key_pem, modulo_evidence_exporter_identity="route-test")


async def _call_pack(
    *,
    key_pem: str,
    stream: Any = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    streamer = stream if stream is not None else (lambda **_: _stream(_event()))
    with (
        patch.object(route, "_audit_session_factory", return_value=MagicMock()),
        patch.object(route, "stream_export_chain", side_effect=streamer),
        patch.object(evidence_signing, "get_settings", lambda: _settings_namespace(key_pem)),
        patch.object(evidence_build, "get_settings", lambda: _settings_namespace(key_pem)),
    ):
        return await route.evidence_pack_endpoint(run_id=run_id, settings=MagicMock(), principal=_principal())


class TestEvidencePackRoute:
    async def test_returns_a_pack_that_verifies_offline(self) -> None:
        private_pem, public_pem = generate_keypair()

        pack = await _call_pack(key_pem=private_pem, run_id="run-42")

        assert pack["manifest"]["organisation_id"] == str(_ORG)
        assert pack["manifest"]["run_ids"] == ["run-42"]
        result = verify_evidence_pack(pack, public_key_pem=public_pem)
        assert result.valid is True
        assert not result.errors

    async def test_signing_unavailable_maps_to_503(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            await _call_pack(key_pem="")
        assert exc_info.value.status_code == 503
        assert "No evidence signing key configured" in str(exc_info.value.detail)

    async def test_programming_error_maps_to_501(self) -> None:
        def _raising(**_: Any) -> AsyncIterator[dict[str, Any]]:
            raise ProgrammingError("stmt", {}, "table not found")

        with pytest.raises(HTTPException) as exc_info:
            await _call_pack(key_pem="unused", stream=_raising)
        assert exc_info.value.status_code == 501

    async def test_sqlalchemy_error_maps_to_503(self) -> None:
        def _raising(**_: Any) -> AsyncIterator[dict[str, Any]]:
            raise SQLAlchemyError("connection failed")

        with pytest.raises(HTTPException) as exc_info:
            await _call_pack(key_pem="unused", stream=_raising)
        assert exc_info.value.status_code == 503

    async def test_truncation_stops_at_the_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(route, "_EVIDENCE_PACK_MAX_EVENTS", 1)
        private_pem, public_pem = generate_keypair()

        pack = await _call_pack(key_pem=private_pem, stream=lambda **_: _stream(_event("a"), _event("b")))

        assert pack["manifest"]["audit_chain"]["truncated"] is True
        assert pack["manifest"]["audit_chain"]["event_count"] == 1
        assert verify_evidence_pack(pack, public_key_pem=public_pem).valid is True


class TestVerificationKeyRoute:
    async def test_publishes_the_pem_and_key_id(self) -> None:
        _, public_pem = generate_keypair()
        with patch.object(route, "configured_public_key_pem", return_value=public_pem):
            payload = await route.verification_key_endpoint()
        assert payload["algorithm"] == "ed25519"
        assert payload["public_key_pem"] == public_pem
        assert isinstance(payload["key_id"], str)

    async def test_unconfigured_key_maps_to_503(self) -> None:
        def _raise() -> str:
            raise EvidenceSigningError("No evidence signing key configured — set MODULO_EVIDENCE_SIGNING_KEY.")

        with (
            patch.object(route, "configured_public_key_pem", side_effect=_raise),
            pytest.raises(HTTPException) as exc_info,
        ):
            await route.verification_key_endpoint()
        assert exc_info.value.status_code == 503
