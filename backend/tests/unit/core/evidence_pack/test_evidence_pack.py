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
    configured_public_key_pem,
    decode_ndjson,
    encode_ndjson,
    key_fingerprint,
    public_key_pem_from_private,
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

    def test_ndjson_ignores_blank_lines(self) -> None:
        events = _make_events()
        blank_padded = encode_ndjson(events[:1]) + b"\n  \n" + encode_ndjson(events[1:])
        assert decode_ndjson(blank_padded) == events

    def test_ndjson_rejects_a_non_json_line(self) -> None:
        with pytest.raises(ValueError, match="non-JSON line"):
            decode_ndjson(b"not json\n")


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


def _rsa_private_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    return (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode()
    )


def _file_entry(pack: dict[str, Any], path: str) -> dict[str, Any]:
    for entry in pack["manifest"]["files"]:
        if entry["path"] == path:
            return entry
    raise AssertionError(f"no manifest entry for {path!r}")


class TestManifestShapeValidation:
    def _pack_and_key(self) -> tuple[dict[str, Any], str]:
        private_pem, public_pem = _keypair()
        return _build(private_pem), public_pem

    def test_audit_chain_that_is_not_an_object_is_reported(self) -> None:
        pack, public_pem = self._pack_and_key()
        pack["manifest"]["audit_chain"] = "not-an-object"

        result = verify_evidence_pack(pack, public_key_pem=public_pem)

        assert result.manifest_ok is False
        assert result.chain_ok is False
        assert any("audit_chain' must be an object" in message for message in result.errors)

    def test_non_mapping_file_entry_is_reported(self) -> None:
        pack, public_pem = self._pack_and_key()
        pack["manifest"]["files"].append("not-an-object")

        result = verify_evidence_pack(pack, public_key_pem=public_pem)

        assert result.manifest_ok is False
        assert result.files_ok is False
        assert any("file entries must be objects" in message for message in result.errors)

    def test_file_entry_without_string_path_or_digest_is_reported(self) -> None:
        pack, public_pem = self._pack_and_key()
        pack["manifest"]["files"].append({"path": None, "sha256": None, "size": 1})

        result = verify_evidence_pack(pack, public_key_pem=public_pem)

        assert result.manifest_ok is False
        assert any("string 'path' and 'sha256'" in message for message in result.errors)

    def test_file_entry_with_non_integer_size_is_reported(self) -> None:
        pack, public_pem = self._pack_and_key()
        pack["manifest"]["files"].append({"path": "p", "sha256": "d", "size": "big"})

        result = verify_evidence_pack(pack, public_key_pem=public_pem)

        assert result.manifest_ok is False
        assert any("integer 'size'" in message for message in result.errors)


class TestAuthorshipBlockValidation:
    def test_non_ed25519_algorithm_is_reported(self) -> None:
        private_pem, public_pem = _keypair()
        pack = _build(private_pem)
        pack["signature"] = {"algorithm": "rsa", "value": "x"}

        result = verify_evidence_pack(pack, public_key_pem=public_pem)

        assert result.authorship_ok is False
        assert any("ed25519 algorithm" in message for message in result.errors)

    def test_missing_signature_value_is_reported(self) -> None:
        private_pem, public_pem = _keypair()
        pack = _build(private_pem)
        pack["signature"] = {"algorithm": "ed25519", "value": ""}

        result = verify_evidence_pack(pack, public_key_pem=public_pem)

        assert result.authorship_ok is False
        assert any("no signature value" in message for message in result.errors)

    def test_manifest_that_cannot_be_canonicalised_is_reported(self) -> None:
        private_pem, public_pem = _keypair()
        pack = _build(private_pem)
        pack["manifest"]["organisation_id"] = {"not", "json", "serialisable"}

        result = verify_evidence_pack(pack, public_key_pem=public_pem)

        assert result.authorship_ok is False
        assert any("cannot be canonicalised" in message for message in result.errors)


class TestFileIntegrityEdges:
    def test_declared_size_that_disagrees_with_digest_is_caught(self) -> None:
        private_pem, public_pem = _keypair()
        pack = _build(private_pem, extra_files={"notes.txt": b"hello"})
        _file_entry(pack, "notes.txt")["size"] += 1

        result = verify_evidence_pack(pack, public_key_pem=public_pem)

        assert result.files_ok is False
        assert any("size does not match" in message for message in result.errors)

    def test_non_string_content_is_treated_as_missing(self) -> None:
        private_pem, public_pem = _keypair()
        pack = _build(private_pem)
        pack["files"][AUDIT_EVENTS_FILE] = {"content_b64": 123}

        result = verify_evidence_pack(pack, public_key_pem=public_pem)

        assert result.files_ok is False
        assert result.chain_ok is False

    def test_invalid_base64_content_is_treated_as_missing(self) -> None:
        private_pem, public_pem = _keypair()
        pack = _build(private_pem)
        pack["files"][AUDIT_EVENTS_FILE] = {"content_b64": "!!! not base64 !!!"}

        result = verify_evidence_pack(pack, public_key_pem=public_pem)

        assert result.files_ok is False
        assert result.chain_ok is False


class TestChainFileEdges:
    def test_missing_events_file_is_caught(self) -> None:
        private_pem, public_pem = _keypair()
        pack = _build(private_pem)
        del pack["files"][AUDIT_EVENTS_FILE]

        result = verify_evidence_pack(pack, public_key_pem=public_pem)

        assert result.chain_ok is False
        assert any("missing the audit-events file" in message for message in result.errors)

    def test_undecodable_events_file_is_caught(self) -> None:
        private_pem, public_pem = _keypair()
        pack = _build(private_pem)
        pack["files"][AUDIT_EVENTS_FILE] = {"content_b64": base64.b64encode(b"this is not json\n").decode("ascii")}

        result = verify_evidence_pack(pack, public_key_pem=public_pem)

        assert result.chain_ok is False
        assert any("could not be decoded" in message for message in result.errors)


class TestEmbeddedKeyCrossCheck:
    def test_empty_embedded_key_is_ignored(self) -> None:
        private_pem, public_pem = _keypair()
        pack = _build(private_pem)
        pack["verification_key"] = {"public_key_pem": ""}

        result = verify_evidence_pack(pack, public_key_pem=public_pem)

        assert result.valid is True
        assert not any("embedded" in message for message in result.errors)

    def test_non_string_embedded_key_is_ignored(self) -> None:
        private_pem, public_pem = _keypair()
        pack = _build(private_pem)
        pack["verification_key"] = {"public_key_pem": 123}

        result = verify_evidence_pack(pack, public_key_pem=public_pem)

        assert result.valid is True

    def test_unusable_embedded_key_is_reported(self) -> None:
        private_pem, public_pem = _keypair()
        pack = _build(private_pem)
        pack["verification_key"] = {"public_key_pem": "not-a-pem"}

        result = verify_evidence_pack(pack, public_key_pem=public_pem)

        assert result.valid is False
        assert any("not a usable PEM" in message for message in result.errors)


class TestChainBreakWithoutId:
    def test_break_on_event_without_id_reports_none(self) -> None:
        chain = recompute_chain([{"previous_hash": "bogus"}], ORG)

        assert chain["valid"] is False
        assert chain["first_gap_index"] == 0
        assert chain["first_tampered_id"] is None


class TestSigningKeyEdges:
    def test_non_ed25519_private_key_cannot_derive_a_public_key(self) -> None:
        with pytest.raises(EvidenceSigningError, match="not an Ed25519 private key"):
            public_key_pem_from_private(_rsa_private_pem())

    def test_configured_public_key_is_derived_from_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        from modulo.core.evidence_pack import signing

        private_pem, public_pem = _keypair()
        monkeypatch.setattr(signing, "get_settings", lambda: SimpleNamespace(modulo_evidence_signing_key=private_pem))

        assert configured_public_key_pem() == public_pem


class TestBuildValidation:
    def test_duplicate_bundle_path_is_rejected(self) -> None:
        private_pem, _ = _keypair()

        with pytest.raises(ValueError, match="duplicate bundle file path"):
            build_evidence_pack(
                organisation_id=ORG,
                events=[],
                extra_files={AUDIT_EVENTS_FILE: b"shadow"},
                private_key_pem=private_pem,
            )

    def test_non_bytes_bundle_content_is_rejected(self) -> None:
        private_pem, _ = _keypair()

        with pytest.raises(TypeError, match="content must be bytes"):
            build_evidence_pack(
                organisation_id=ORG,
                events=[],
                extra_files={"notes.txt": "not-bytes"},  # type: ignore[dict-item]
                private_key_pem=private_pem,
            )


class TestEvidenceCommandRegistration:
    def test_conflicting_registration_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import modulo.cli.main as cli_main

        monkeypatch.setitem(cli_main.cli.commands, "evidence", object())

        with pytest.raises(RuntimeError, match="already registered"):
            cli_main._init_once_register_evidence_command()


class TestEvidenceCliErrors:
    def _write_key(self, tmp_path: Path) -> Path:
        _, public_pem = _keypair()
        key_path = tmp_path / "key.pem"
        key_path.write_text(public_pem, encoding="utf-8")
        return key_path

    def test_non_json_pack_is_reported(self, tmp_path: Path) -> None:
        pack_path = tmp_path / "pack.json"
        pack_path.write_text("{ not json", encoding="utf-8")
        key_path = self._write_key(tmp_path)

        result = CliRunner().invoke(evidence_cli, ["verify", str(pack_path), "--public-key", str(key_path)])

        assert result.exit_code != 0
        assert "could not read the evidence pack" in result.output

    def test_non_object_pack_root_is_reported(self, tmp_path: Path) -> None:
        pack_path = tmp_path / "pack.json"
        pack_path.write_text("[1, 2, 3]", encoding="utf-8")
        key_path = self._write_key(tmp_path)

        result = CliRunner().invoke(evidence_cli, ["verify", str(pack_path), "--public-key", str(key_path)])

        assert result.exit_code != 0
        assert "must be a JSON object" in result.output

    def test_unreadable_public_key_is_reported(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        private_pem, public_pem = _keypair()
        pack_path = tmp_path / "pack.json"
        pack_path.write_text(json.dumps(_build(private_pem)), encoding="utf-8")
        key_path = tmp_path / "key.pem"
        key_path.write_text(public_pem, encoding="utf-8")

        real_read_text = Path.read_text

        def _fake_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
            if self == key_path:
                raise OSError("permission denied")
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _fake_read_text)

        result = CliRunner().invoke(evidence_cli, ["verify", str(pack_path), "--public-key", str(key_path)])

        assert result.exit_code != 0
        assert "could not read the public key" in result.output

    def test_human_output_lists_errors(self, tmp_path: Path) -> None:
        private_pem, public_pem = _keypair()
        pack = _build(private_pem)
        pack["manifest"]["created_at"] = "2030-01-01T00:00:00+00:00"
        pack_path = tmp_path / "pack.json"
        pack_path.write_text(json.dumps(pack), encoding="utf-8")
        key_path = tmp_path / "key.pem"
        key_path.write_text(public_pem, encoding="utf-8")

        result = CliRunner().invoke(evidence_cli, ["verify", str(pack_path), "--public-key", str(key_path)])

        assert result.exit_code == 1
        assert "INVALID" in result.output
        assert "error:" in result.output


class TestEvidencePublicKeyCli:
    def test_prints_the_configured_pem(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, public_pem = _keypair()
        monkeypatch.setattr("modulo.core.evidence_pack.configured_public_key_pem", lambda: public_pem)

        result = CliRunner().invoke(evidence_cli, ["public-key"])

        assert result.exit_code == 0
        assert public_pem.strip() in result.output

    def test_json_flag_emits_the_key_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, public_pem = _keypair()
        monkeypatch.setattr("modulo.core.evidence_pack.configured_public_key_pem", lambda: public_pem)

        result = CliRunner().invoke(evidence_cli, ["public-key", "--json"])

        assert result.exit_code == 0
        assert '"algorithm": "ed25519"' in result.output
        assert key_fingerprint(public_pem) in result.output

    def test_unconfigured_key_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise() -> str:
            raise EvidenceSigningError("No evidence signing key configured")

        monkeypatch.setattr("modulo.core.evidence_pack.configured_public_key_pem", _raise)

        result = CliRunner().invoke(evidence_cli, ["public-key"])

        assert result.exit_code != 0
        assert "No evidence signing key configured" in result.output
