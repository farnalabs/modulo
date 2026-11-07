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

import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
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


def _python_bin() -> str:
    """The interpreter the extracted signature parser is EXECUTED with.

    ``install.sh`` itself shells out to ``python3``, which is correct in the
    installer's Linux environment but is not a name that exists on a Windows
    host (which has only ``python`` / ``py``). These tests run the parser's
    *body* in a Python process, so the interpreter under test is the one
    actually running pytest — ``sys.executable``. A PATH lookup is only a
    fallback for a degenerate embedded interpreter with an empty
    ``sys.executable``; skipping is a last resort, never the Windows outcome.
    """
    if sys.executable:
        return sys.executable
    resolved = shutil.which("python3") or shutil.which("python")
    if resolved is None:
        pytest.skip("no Python interpreter available to execute install.sh's extracted signature parser")
    return resolved


def _run_signature_parser(
    signature_text: str,
    tmp_path: Path,
    *,
    current_id: str = "modulo-2026-a",
    next_id: str = "modulo-2026-a-next",
) -> subprocess.CompletedProcess[str]:
    source = _INSTALL_SH.read_text(encoding="utf-8")
    match = re.search(
        r"signature_fields=\"\$\(python3 .*?<<'PYEOF'\n(?P<body>.*?)\nPYEOF",
        source,
        re.DOTALL,
    )
    assert match is not None, "install.sh must retain the extracted signature parser"
    parser_source = match.group("body")
    signature_file = tmp_path / "signature-parser-fixture.json"
    try:
        signature_file.write_text(signature_text, encoding="utf-8")
        return subprocess.run(  # noqa: S603 - resolved Python interpreter and temporary fixture paths
            [_python_bin(), "-", str(signature_file), current_id, next_id],
            input=parser_source,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    finally:
        signature_file.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "signature_text",
    [
        "{}",
        '{"signature":"' + "0" * 128 + '"}',
        '{"key_id":"modulo-2026-a","signature":"' + "0" * 128 + '","key_id":"rogue"}',
        '{"key_id":"modulo-2026-a","signature":"not-hex"}',
    ],
)
# POSIX-only (this module is mixed): the tests below execute the shipped shell
# script for real, so each carries ``pytest.mark.posix_only``. The gate that skips
# them on Windows lives in ``backend/tests/conftest.py``; the module's remaining
# tests assert on workflow/file text and still run on Windows.
@pytest.mark.posix_only
def test_installer_signature_parser_executes_rejection_for_malformed_envelopes(
    signature_text: str, tmp_path: Path
) -> None:
    result = _run_signature_parser(signature_text, tmp_path)
    assert result.returncode != 0
    output = result.stderr + result.stdout
    assert output


@pytest.mark.parametrize("key_id", ["modulo-2026-a", "modulo-2026-a-next"])
@pytest.mark.posix_only
def test_installer_signature_parser_accepts_current_and_next_key_ids(key_id: str, tmp_path: Path) -> None:
    signature_text = '{"key_id":"' + key_id + '","signature":"' + "a" * 128 + '"}'
    result = _run_signature_parser(signature_text, tmp_path)
    assert result.returncode == 0
    assert result.stdout.splitlines() == [key_id, "a" * 128]


def test_installer_signature_parser_preserves_explicit_rotation_slots() -> None:
    source = _INSTALL_SH.read_text(encoding="utf-8")
    assert '"${TRUST_KEY_ID_CURRENT}") candidate_keys="current"' in source
    assert '"${TRUST_KEY_ID_NEXT}") candidate_keys="next"' in source
    assert "unknown signing key-id" in source


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


# ---------------------------------------------------------------------------
# FAR-2912 — a bare `--from-file` name must resolve the manifest BESIDE it.
# ---------------------------------------------------------------------------

_ENV_KEYS_TO_DROP = (
    "XDG_STATE_HOME",
    "XDG_DATA_HOME",
    "VERSION",
    "MODULO_INSTALL_ROOT",
    "MODULO_BIN_DIR",
    "MODULO_DATA_DIR",
    "MODULO_LEGACY_SHIM_PATH",
)


def _bash() -> str:
    resolved = shutil.which("bash")
    assert resolved is not None, "the installer is a bash script; bash must be on PATH to test it"
    return resolved


def _bare_tarball_name() -> str:
    system = platform.system()
    machine = platform.machine().lower()
    archive_os = "darwin" if system == "Darwin" else "linux"
    archive_arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    assert archive_arch is not None, f"unsupported test host architecture: {machine}"
    return f"modulo-1.0.0-{archive_os}-{archive_arch}.tar.gz"


def _run_installer_from_cwd(tmp_path: Path, tarball_arg: str) -> subprocess.CompletedProcess[str]:
    """Run the real installer from ``tmp_path`` so a BARE ``--from-file`` resolves there."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {key: value for key, value in os.environ.items() if key not in _ENV_KEYS_TO_DROP}
    env.update(
        {
            "HOME": str(home),
            "MODULO_INSTALL_ROOT": str(tmp_path / "install-root"),
            "MODULO_BIN_DIR": str(tmp_path / "bin"),
            "MODULO_LEGACY_SHIM_PATH": str(tmp_path / "no-legacy-shim"),
        }
    )
    return subprocess.run(  # noqa: S603 - resolved bash and test-owned paths
        [_bash(), str(_INSTALL_SH), "--from-file", tarball_arg],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )


@pytest.mark.posix_only
def test_from_file_bare_filename_resolves_the_manifest_beside_the_tarball(tmp_path: Path) -> None:
    """The canary leg passes a bare filename; ``${FROM_FILE%/*}`` must not become the tarball itself."""
    tarball_name = _bare_tarball_name()
    (tmp_path / tarball_name).write_bytes(b"placeholder")
    result = _run_installer_from_cwd(tmp_path, tarball_name)
    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "offline install REFUSED" in combined
    assert f"{tarball_name}/RELEASE_MANIFEST.json" not in combined, (
        "install.sh treated the bare tarball filename as the manifest's directory:\n" + combined
    )
    assert "./RELEASE_MANIFEST.json" in combined


@pytest.mark.posix_only
def test_from_file_bare_filename_finds_an_existing_manifest(tmp_path: Path) -> None:
    """A manifest beside a bare-named tarball is found (no 'must sit beside the tarball' refusal)."""
    tarball_name = _bare_tarball_name()
    (tmp_path / tarball_name).write_bytes(b"placeholder")
    (tmp_path / "RELEASE_MANIFEST.json").write_text("{}", encoding="utf-8")
    (tmp_path / "RELEASE_MANIFEST.json.sig").write_text('{"key_id": "modulo-2026-a"}', encoding="utf-8")
    result = _run_installer_from_cwd(tmp_path, tarball_name)
    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "offline install REFUSED" not in combined, combined
    assert "the signed release manifest is missing" not in combined, combined
    assert "the signature file is missing" not in combined, combined
