"""FAR-2182 — the installer's openssl manifest-signature argv is EXECUTED.

``scripts/install.sh`` verifies the signed release manifest by shelling out to
``openssl pkeyutl``. This suite extracts that exact argv from the script and
runs it against a real ed25519 keypair and manifest, so the gate covers the
ARGV ITSELF — not merely the presence of a string in the script.

The bug this locks (FAR-2182): the argv carried ``-inform DER``, an option
``pkeyutl`` does not accept (the public key is supplied with ``-inkey`` +
``-keyform``). Every verification therefore exited 1 with
``pkeyutl: Unknown option: -inform`` while stderr went to ``/dev/null`` and the
operator was told the manifest had failed to verify against the trust store —
sending them hunting for a key mismatch that did not exist.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

_REPO_ROOT = Path(__file__).resolve().parents[4]
_INSTALL_SH = _REPO_ROOT / "scripts" / "install.sh"
_OPENSSL_MARKER = "openssl pkeyutl"
_PLACEHOLDERS = {
    "${key_file}": "key",
    "${manifest_file}": "manifest",
    "${sig_file}": "signature",
}
# The historical (FAR-2182) argv, kept verbatim so the suite proves that the
# malformed shape is rejected when EXECUTED — a string assertion cannot.
_LEGACY_INFORM_ARGV = (
    'openssl pkeyutl -verify -pubin -inform DER -in "${key_file}" -rawin -in "${manifest_file}" -sigfile "${sig_file}"'
)


@dataclass(frozen=True)
class _SignedManifest:
    key_der: Path
    manifest: Path
    signature: Path
    payload: bytes


def _openssl_or_skip() -> str:
    resolved = shutil.which("openssl")
    if resolved is None:
        pytest.skip("openssl is not installed; install.sh requires OpenSSL 3.0+ at install time")
    version = subprocess.run(  # noqa: S603 - resolved absolute path to the system openssl
        [resolved, "version"], capture_output=True, text=True, check=True, timeout=30
    )
    tokens = version.stdout.split()
    assert "OpenSSL" in tokens, f"unexpected `openssl version` output: {version.stdout!r}"
    major = int(tokens[tokens.index("OpenSSL") + 1].split(".")[0])
    if major < 3:
        pytest.skip(f"install.sh requires OpenSSL 3.0+ (`pkeyutl -rawin`); this host has {major}.x")
    return resolved


@pytest.fixture(scope="module")
def openssl_bin() -> str:
    return _openssl_or_skip()


@pytest.fixture
def signed_manifest(tmp_path: Path) -> _SignedManifest:
    """An ed25519 keypair, a manifest and a signature over its exact bytes."""
    private_key = Ed25519PrivateKey.generate()
    key_der = tmp_path / "trust-key.der"
    key_der.write_bytes(private_key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo))
    payload = b'{\n  "manifest_version": 1,\n  "release": "bundle-v1.2.0"\n}\n'
    manifest = tmp_path / "RELEASE_MANIFEST.json"
    manifest.write_bytes(payload)
    signature = tmp_path / "signature.bin"
    signature.write_bytes(private_key.sign(payload))
    return _SignedManifest(key_der=key_der, manifest=manifest, signature=signature, payload=payload)


def _openssl_argv_tokens() -> list[str]:
    """Extract the ``openssl pkeyutl -verify ...`` argv from install.sh.

    Fails loudly when the script no longer carries exactly one such command —
    the extraction is the link between the production script and this gate.
    """
    lines = [
        line
        for line in _INSTALL_SH.read_text(encoding="utf-8").splitlines()
        if _OPENSSL_MARKER in line and "-verify" in line
    ]
    assert len(lines) == 1, f"expected exactly one `openssl pkeyutl -verify` line in install.sh, found {len(lines)}"
    command = lines[0][lines[0].index(_OPENSSL_MARKER) :]
    for redirect in (" > ", " 2>", " 1>"):
        cut = command.find(redirect)
        if cut != -1:
            command = command[:cut]
    return shlex.split(command)


def _substitute(tokens: list[str], *, key: Path, manifest: Path, signature: Path) -> list[str]:
    paths = {"key": str(key), "manifest": str(manifest), "signature": str(signature)}
    argv: list[str] = []
    for token in tokens:
        slot = _PLACEHOLDERS.get(token)
        argv.append(paths[slot] if slot is not None else token)
    unresolved = [token for token in argv if "${" in token]
    assert not unresolved, f"install.sh's openssl argv carries unresolved placeholders: {unresolved}"
    return argv


def _run(argv: list[str], openssl_bin: str) -> subprocess.CompletedProcess[str]:
    command = list(argv)
    if Path(command[0]).name == "openssl":
        command[0] = openssl_bin
    return subprocess.run(  # noqa: S603 - literal binary + paths from this test's tmp dir
        command,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _installer_argv(signed_manifest: _SignedManifest) -> list[str]:
    return _substitute(
        _openssl_argv_tokens(),
        key=signed_manifest.key_der,
        manifest=signed_manifest.manifest,
        signature=signed_manifest.signature,
    )


def test_extracted_argv_verifies_a_signed_manifest(signed_manifest: _SignedManifest, openssl_bin: str) -> None:
    """The argv install.sh actually runs must verify a correctly signed manifest."""
    result = _run(_installer_argv(signed_manifest), openssl_bin)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"install.sh's openssl argv failed a VALID signature:\n{combined}"
    assert "Signature Verified Successfully" in combined


def test_extracted_argv_rejects_a_tampered_manifest(signed_manifest: _SignedManifest, openssl_bin: str) -> None:
    """A tampered manifest must fail for the SIGNATURE reason, not a bad argv."""
    signed_manifest.manifest.write_bytes(signed_manifest.payload + b"tampered\n")
    result = _run(_installer_argv(signed_manifest), openssl_bin)
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Unknown option" not in combined, f"openssl rejected an option, not the signature:\n{combined}"
    assert "Signature Verification Failure" in combined


def test_extracted_argv_rejects_a_wrong_trust_key(signed_manifest: _SignedManifest, openssl_bin: str) -> None:
    """A key that did not sign the manifest must fail for the SIGNATURE reason."""
    signed_manifest.key_der.write_bytes(
        Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    )
    result = _run(_installer_argv(signed_manifest), openssl_bin)
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Unknown option" not in combined, f"openssl rejected an option, not the signature:\n{combined}"
    assert "Signature Verification Failure" in combined


def test_extracted_argv_uses_pkeyutl_key_options() -> None:
    """The argv's option shape: -inkey + -keyform, ONE -in, never -inform."""
    tokens = _openssl_argv_tokens()
    assert "-verify" in tokens
    assert "-pubin" in tokens
    assert "-rawin" in tokens
    assert "-inform" not in tokens, "pkeyutl has no -inform option (that is why the original argv never verified)"
    assert "-keyform" in tokens
    assert tokens[tokens.index("-keyform") + 1] == "DER"
    assert tokens.count("-in") == 1, f"pkeyutl takes the signed bytes via ONE -in; got {tokens.count('-in')}"
    assert "-inkey" in tokens
    assert "-sigfile" in tokens


def test_the_legacy_inform_argv_is_rejected_when_executed(signed_manifest: _SignedManifest, openssl_bin: str) -> None:
    """Proof the gate detects the FAR-2182 bug class by RUNNING the argv.

    The historical argv is executed against a perfectly valid signature: if
    the suite only string-matched the script, this shape would look fine.
    """
    argv = _substitute(
        shlex.split(_LEGACY_INFORM_ARGV),
        key=signed_manifest.key_der,
        manifest=signed_manifest.manifest,
        signature=signed_manifest.signature,
    )
    result = _run(argv, openssl_bin)
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Unknown option: -inform" in combined
