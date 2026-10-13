"""Tests: offline-verifiable evidence packs, Ed25519 signed (FAR-1697)."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from modulo.cli.evidence import evidence as evidence_cli
from modulo.core.audit_logger import compute_event_hash
from modulo.core.audit_logger.hashing import compute_event_hash as direct_compute_event_hash
from modulo.core.evidence_pack import (
    AUDIT_EVENTS_FILE,
    EvidenceSigningError,
    build_evidence_pack,
    canonical_json_bytes,
    decode_ndjson,
    encode_ndjson,
    key_fingerprint,
    recompute_chain,
    sign_manifest,
    verify_evidence_pack,
)
from modulo.registry.crypto import generate_keypair

ORG = "00000000-0000-0000-0000-0000000000aa"
CREATED_AT = "2026-10-13T12:00:00+00:00"


def _make_events() -> list[dict[str, Any]]:
    """A two-event, correctly-linked audit chain, in the export dict shape."""
    events: list[dict[str, Any]] = []
    previous: str | None = None
    specs = (("event.a", {}), ("event.b", {"x": 1}))
    for index, (event_type, payload) in enumerate(specs, start=1):
        event_id = f"00000000-0000-0000-0000-00000000000{index}"
        created_at = f"2026-01-0{index}T00:00:00+00:00"
        previous_hash = previous
        previous = compute_event_hash(
            event_type, None, None, None, payload, None, previous_hash, event_id, ORG, created_at
        )
        events.append(
            {
                "id": event_id,
                "event_type": event_type,
                "actor_user_id": None,
                "resource_type": None,
                "resource_id": None,
                "payload_json": payload,
                "request_id": None,
                "previous_hash": previous_hash,
                "created_at": created_at,
            }
        )
    return events


def _keypair() -> tuple[str, str]:
    private_pem, public_pem = generate_keypair()
    return private_pem, public_pem


def _build(
    private_pem: str,
    *,
    events: list[dict[str, Any]] | None = None,
    extra_files: dict[str, bytes] | None = None,
) -> dict[str, Any]:
    return build_evidence_pack(
        organisation_id=ORG,
        events=events if events is not None else _make_events(),
        extra_files=extra_files,
        run_ids=["run-1"],
        exporter_identity="test-suite",
        created_at=CREATED_AT,
        private_key_pem=private_pem,
    )


class TestSignVerifyRoundTrip:
    def test_valid_pack_verifies_with_only_artefact_and_key(self) -> None:
        private_pem, public_pem = _keypair()
        pack = _build(private_pem)

        result = verify_evidence_pack(pack, public_key_pem=public_pem)

        assert result.valid is True
        assert result.authorship_ok is True
        assert result.files_ok is True
        assert result.chain_ok is True
        assert result.manifest_ok is True
        assert not result.errors

    def test_pack_is_self_describing_and_json_serialisable(self) -> None:
        private_pem, public_pem = _keypair()
        pack = _build(private_pem)

        assert pack["signature"]["algorithm"] == "ed25519"
        assert pack["signature"]["key_id"] == key_fingerprint(public_pem)
        assert pack["verification_key"]["public_key_pem"] == public_pem
        assert AUDIT_EVENTS_FILE in pack["files"]
        # Round-trips through the wire form the CLI reads.
        assert json.loads(json.dumps(pack))["manifest"] == pack["manifest"]

    def test_key_fingerprint_is_stable_and_short(self) -> None:
        _, public_pem = _keypair()
        assert key_fingerprint(public_pem) == key_fingerprint(public_pem)
        assert len(key_fingerprint(public_pem)) == 16


class TestAuthorship:
    def test_verify_fails_under_a_different_key(self) -> None:
        private_pem, _ = _keypair()
        _, other_public = _keypair()
        pack = _build(private_pem)

        result = verify_evidence_pack(pack, public_key_pem=other_public)

        assert result.valid is False
        assert result.authorship_ok is False

    def test_manifest_tamper_after_signing_fails_authorship(self) -> None:
        private_pem, public_pem = _keypair()
        pack = _build(private_pem)
        pack["manifest"]["created_at"] = "2030-01-01T00:00:00+00:00"

        result = verify_evidence_pack(pack, public_key_pem=public_pem)

        assert result.authorship_ok is False
        assert result.valid is False

    def test_embedded_key_swap_is_reported(self) -> None:
        private_pem, public_pem = _keypair()
        _, other_public = _keypair()
        pack = _build(private_pem)
        pack["verification_key"]["public_key_pem"] = other_public

        result = verify_evidence_pack(pack, public_key_pem=public_pem)

        assert result.valid is False
        assert any("embedded verification key" in message for message in result.errors)


class TestFileIntegrity:
    def test_digest_mismatch_on_an_extra_file_is_caught(self) -> None:
        private_pem, public_pem = _keypair()
        pack = _build(private_pem, extra_files={"notes.txt": b"hello"})
        pack["files"]["notes.txt"]["content_b64"] = base64.b64encode(b"HELLO").decode("ascii")

        result = verify_evidence_pack(pack, public_key_pem=public_pem)

        assert result.files_ok is False
        assert result.valid is False

    def test_missing_listed_file_is_caught(self) -> None:
        private_pem, public_pem = _keypair()
        pack = _build(private_pem, extra_files={"notes.txt": b"hello"})
        del pack["files"]["notes.txt"]

        result = verify_evidence_pack(pack, public_key_pem=public_pem)

        assert result.files_ok is False

    def test_unlisted_file_is_caught(self) -> None:
        private_pem, public_pem = _keypair()
        pack = _build(private_pem)
        pack["files"]["smuggled.txt"] = {"content_b64": base64.b64encode(b"x").decode("ascii")}

        result = verify_evidence_pack(pack, public_key_pem=public_pem)

        assert result.files_ok is False


class TestChainReverification:
    def test_tampered_chain_claim_is_caught_even_when_re_signed(self) -> None:
        """A malicious exporter that lies about the chain cannot pass."""
        private_pem, public_pem = _keypair()
        pack = _build(private_pem)
        # Falsify the signed chain claim, then re-sign so authorship still holds
        # — proving the verifier catches the lie, not merely a broken signature.
        pack["manifest"]["audit_chain"]["head_hash"] = "0" * 64
        pack["manifest"]["audit_chain"]["valid"] = False
        pack["signature"] = sign_manifest(pack["manifest"], private_pem)

        result = verify_evidence_pack(pack, public_key_pem=public_pem)

        assert result.authorship_ok is True
        assert result.files_ok is True
        assert result.chain_ok is False
        assert result.valid is False
        assert any("audit chain" in message for message in result.errors)

    def test_broken_link_is_detected_by_recompute(self) -> None:
        events = _make_events()
        events[1]["previous_hash"] = "deadbeef"

        chain = recompute_chain(events, ORG)

        assert chain["valid"] is False
        assert chain["first_gap_index"] == 1
        assert chain["first_tampered_id"] == events[1]["id"]

    def test_empty_chain_is_a_valid_empty_chain(self) -> None:
        chain = recompute_chain([], ORG)

        assert chain["valid"] is True
        assert chain["head_hash"] is None
        assert chain["event_count"] == 0

    def test_mutated_event_payload_changes_the_recomputed_head(self) -> None:
        events = _make_events()
        original = recompute_chain(events, ORG)["head_hash"]
        events[0]["payload_json"] = {"tampered": True}

        assert recompute_chain(events, ORG)["head_hash"] != original


class TestCanonicalSerialisation:
    def test_key_order_does_not_change_the_canonical_bytes(self) -> None:
        first = {"b": 2, "a": 1}
        second = {"a": 1, "b": 2}
        assert canonical_json_bytes(first) == canonical_json_bytes(second)

    def test_ndjson_round_trips(self) -> None:
        events = _make_events()
        assert decode_ndjson(encode_ndjson(events)) == events

    def test_ndjson_rejects_a_non_object_record(self) -> None:
        with pytest.raises(ValueError, match="non-object record"):
            decode_ndjson(b"[1, 2, 3]\n")


class TestSigningFailsClosed:
    def test_build_without_a_configured_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        from modulo.core.evidence_pack import signing

        monkeypatch.setattr(signing, "get_settings", lambda: SimpleNamespace(modulo_evidence_signing_key=""))

        with pytest.raises(EvidenceSigningError, match="No evidence signing key configured"):
            build_evidence_pack(organisation_id=ORG, events=[], private_key_pem=None)

    def test_non_ed25519_key_is_rejected(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        rsa_pem = (
            rsa.generate_private_key(public_exponent=65537, key_size=2048)
            .private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            .decode()
        )

        with pytest.raises(EvidenceSigningError, match="could not sign the evidence manifest"):
            build_evidence_pack(
                organisation_id=ORG,
                events=[],
                exporter_identity="t",
                created_at=CREATED_AT,
                private_key_pem=rsa_pem,
            )


class TestHostileInput:
    def test_empty_pack_does_not_raise(self) -> None:
        _, public_pem = _keypair()
        result = verify_evidence_pack({}, public_key_pem=public_pem)
        assert result.valid is False
        assert not result.manifest_ok

    def test_malformed_manifest_shape_is_reported(self) -> None:
        _, public_pem = _keypair()
        pack: dict[str, Any] = {
            "manifest": {"files": "not-a-list"},
            "signature": {"algorithm": "ed25519", "value": "x"},
            "files": {},
        }
        result = verify_evidence_pack(pack, public_key_pem=public_pem)
        assert result.valid is False
        assert result.manifest_ok is False


class TestHashingIsSharedWithAuditLogger:
    def test_alias_is_the_same_implementation(self) -> None:
        assert compute_event_hash is direct_compute_event_hash


class TestEvidenceCli:
    def test_verify_command_exits_zero_for_a_valid_pack(self, tmp_path: Path) -> None:
        private_pem, public_pem = _keypair()
        pack = _build(private_pem)
        pack_path = tmp_path / "pack.json"
        key_path = tmp_path / "key.pem"
        pack_path.write_text(json.dumps(pack), encoding="utf-8")
        key_path.write_text(public_pem, encoding="utf-8")

        result = CliRunner().invoke(evidence_cli, ["verify", str(pack_path), "--public-key", str(key_path)])

        assert result.exit_code == 0
        assert "VALID" in result.output

    def test_verify_command_exits_nonzero_for_a_tampered_pack(self, tmp_path: Path) -> None:
        private_pem, public_pem = _keypair()
        pack = _build(private_pem)
        pack["manifest"]["created_at"] = "2030-01-01T00:00:00+00:00"
        pack_path = tmp_path / "pack.json"
        key_path = tmp_path / "key.pem"
        pack_path.write_text(json.dumps(pack), encoding="utf-8")
        key_path.write_text(public_pem, encoding="utf-8")

        result = CliRunner().invoke(evidence_cli, ["verify", str(pack_path), "--public-key", str(key_path), "--json"])

        assert result.exit_code == 1
        assert '"valid": false' in result.output
