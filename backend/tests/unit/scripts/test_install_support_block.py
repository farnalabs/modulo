"""FAR-2303 — the installer persists its log and prints a support reference.

``scripts/install.sh`` streams everything to stdout, so a failed cold install
left the operator with scrolling terminal output and no ``modulo`` binary to
run ``modulo doctor --report`` with. This suite pins the replacement contract
by EXECUTING the real installer (never by string-matching it), in an isolated
``HOME``:

* every run tees its transcript into ``~/.local/state/modulo/install-<UTC>-<ref>.log``
  (state dir 0700, log 0600);
* the transcript is size-capped and only the newest 5 logs are kept;
* EVERY failure path — ``die``, a bare ``exit 1`` inside ``fetch``, an
  unsupported platform — prints one delimited support block as its LAST
  output, naming the target, the failing stage, the absolute log path and an
  8-hex support reference that is also in the log filename;
* a successful run (``--help``) prints no block;
* exit codes are unchanged.

The extraction test at the bottom runs the installer's ``redact()`` helper
directly: the block and log must never carry a credential or signed-URL
secret.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_INSTALL_SH = _REPO_ROOT / "scripts" / "install.sh"

#: This module EXECUTES the shipped installer under a real POSIX shell, so it
#: needs a POSIX shell rather than the Windows WSL launcher stub. The platform
#: requirement is declared once, as a marker; the gate that skips it on Windows
#: lives in ``backend/tests/conftest.py`` (``POSIX_ONLY_MARKER``) and Linux CI
#: executes this suite for real.
pytestmark = pytest.mark.posix_only

_BLOCK_START = re.compile(r"^----- Modulo install failed: support reference (?P<ref>[0-9a-f]{8}) -----$")
_BLOCK_END = "----- end of Modulo install support block -----"
_REF_PATTERN = re.compile(r"^[0-9a-f]{8}$")
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


def _install_env(tmp_path: Path, *, path_prefix: Path | None = None) -> dict[str, str]:
    """An isolated environment: private HOME, install root and shim all in tmp."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {key: value for key, value in os.environ.items() if key not in _ENV_KEYS_TO_DROP}
    env.update(
        {
            "HOME": str(home),
            "MODULO_INSTALL_ROOT": str(tmp_path / "install-root"),
            "MODULO_BIN_DIR": str(tmp_path / "bin"),
            # A real /usr/local/bin/modulo on the host must not mask the
            # failure this test is inducing.
            "MODULO_LEGACY_SHIM_PATH": str(tmp_path / "no-legacy-shim"),
        }
    )
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env.get('PATH', '')}"
    return env


def _run_installer(
    script: Path,
    args: list[str],
    env: dict[str, str],
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    run_env = dict(env)
    if extra_env:
        run_env.update(extra_env)
    return subprocess.run(  # noqa: S603 - resolved bash and test-owned paths
        [_bash(), str(script), *args],
        capture_output=True,
        text=True,
        env=run_env,
        timeout=120,
        check=False,
    )


def _state_dir(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".local" / "state" / "modulo"


def _logs(tmp_path: Path) -> list[Path]:
    state = _state_dir(tmp_path)
    if not state.is_dir():
        return []
    return sorted(state.glob("install-*.log"))


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def _last_non_empty_line(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines, f"the installer produced no output at all:\n{text}"
    return lines[-1]


def _block_field(text: str, label: str) -> str:
    pattern = re.compile(rf"^  {re.escape(label)}\s*:\s*(?P<value>.+)$", re.MULTILINE)
    match = pattern.search(text)
    assert match is not None, f"the support block has no '{label}' line:\n{text}"
    return match.group("value").strip()


def _stub(tmp_path: Path, name: str, body: str) -> Path:
    """A stub executable on PATH, for inducing failures without touching the repo."""
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / name
    stub.write_text(body, encoding="utf-8")
    stub.chmod(0o755)
    return bin_dir


# ---------------------------------------------------------------------------
# The happy-path contract: nothing printed, nothing to send.
# ---------------------------------------------------------------------------


# POSIX-only (this module is mixed): the tests below execute the shipped shell
# script for real, so each carries ``pytest.mark.posix_only``. The gate that skips
# them on Windows lives in ``backend/tests/conftest.py``; the module's remaining
# tests assert on workflow/file text and still run on Windows.
@pytest.mark.posix_only
def test_help_exits_zero_without_a_support_block(tmp_path: Path) -> None:
    result = _run_installer(_INSTALL_SH, ["--help"], _install_env(tmp_path))
    assert result.returncode == 0
    combined = _output(result)
    assert _BLOCK_END not in combined
    assert "support reference" not in combined


# ---------------------------------------------------------------------------
# Every failure path: one delimited block, printed LAST.
# ---------------------------------------------------------------------------


@pytest.mark.posix_only
def test_bad_version_prints_the_support_block_last(tmp_path: Path) -> None:
    result = _run_installer(_INSTALL_SH, [], _install_env(tmp_path), {"VERSION": "nonsense"})
    assert result.returncode == 1
    combined = _output(result)
    assert _BLOCK_END in combined
    assert _last_non_empty_line(combined) == _BLOCK_END


@pytest.mark.posix_only
def test_support_block_names_target_stage_log_and_reference(tmp_path: Path) -> None:
    result = _run_installer(_INSTALL_SH, [], _install_env(tmp_path), {"VERSION": "nonsense"})
    combined = _output(result)
    assert _BLOCK_END in combined

    reference = _block_field(combined, "Support reference")
    assert _REF_PATTERN.match(reference), f"support reference is not 8 hex chars: {reference!r}"

    assert _block_field(combined, "Install target") == "nonsense"
    assert _block_field(combined, "Failing stage") == "resolving the release to install"

    log_path = Path(_block_field(combined, "Install log"))
    assert log_path.is_absolute()
    assert log_path.is_file()
    assert log_path.name.endswith(f"-{reference}.log"), "the reference must be embedded in the log filename"
    assert log_path.parent == _state_dir(tmp_path)


@pytest.mark.posix_only
def test_transcript_is_persisted_with_private_permissions(tmp_path: Path) -> None:
    result = _run_installer(_INSTALL_SH, [], _install_env(tmp_path), {"VERSION": "nonsense"})
    assert result.returncode == 1

    log_path = Path(_block_field(_output(result), "Install log"))
    transcript = log_path.read_text(encoding="utf-8")
    assert "ERROR: VERSION='nonsense' is not a bundle tag" in transcript
    assert _BLOCK_END in transcript
    if os.name == "posix":
        assert log_path.stat().st_mode & 0o777 == 0o600
        assert _state_dir(tmp_path).stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    ("args", "extra_env"),
    [
        ([], {"VERSION": "nonsense"}),
        (["--from-file", "/nonexistent/modulo-1.0.0-linux-amd64.tar.gz"], {}),
        (["--unknown-option"], {}),
    ],
    ids=["bad-version", "missing-from-file", "unknown-argument"],
)
@pytest.mark.posix_only
def test_failure_exit_codes_are_unchanged(tmp_path: Path, args: list[str], extra_env: dict[str, str]) -> None:
    """The support block must not change what the installer returns today."""
    result = _run_installer(_INSTALL_SH, args, _install_env(tmp_path), extra_env)
    assert result.returncode == 1
    assert _BLOCK_END in _output(result)


@pytest.mark.posix_only
def test_unsupported_architecture_prints_the_block(tmp_path: Path) -> None:
    """A common early exit (not a `die` in a later stage) must still print it."""
    uname_stub = (
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        '  -s) printf "Linux\\n" ;;\n'
        '  -m) printf "s390x\\n" ;;\n'
        '  *) printf "Linux\\n" ;;\n'
        "esac\n"
    )
    bin_dir = _stub(tmp_path, "uname", uname_stub)
    result = _run_installer(_INSTALL_SH, [], _install_env(tmp_path, path_prefix=bin_dir))
    assert result.returncode == 1
    combined = _output(result)
    assert "unsupported architecture: s390x" in combined
    assert _last_non_empty_line(combined) == _BLOCK_END
    assert _block_field(combined, "Failing stage") == "preflight (platform and tool checks)"
    assert _block_field(combined, "Install target") == "latest (not yet resolved)"


@pytest.mark.posix_only
def test_download_failure_names_the_download_stage(tmp_path: Path) -> None:
    """`fetch` fails with a bare `exit 1` — not `die` — and must still be covered."""
    bin_dir = _stub(tmp_path, "curl", "#!/usr/bin/env bash\nexit 6\n")
    result = _run_installer(
        _INSTALL_SH,
        [],
        _install_env(tmp_path, path_prefix=bin_dir),
        {"VERSION": "bundle-v0.0.1"},
    )
    assert result.returncode == 1
    combined = _output(result)
    assert "download failed (curl exit 6)" in combined
    assert _block_field(combined, "Failing stage") == "download"
    assert _block_field(combined, "Install target") == "bundle-v0.0.1 (linux-amd64)"
    assert _last_non_empty_line(combined) == _BLOCK_END


@pytest.mark.posix_only
def test_each_run_gets_its_own_support_reference(tmp_path: Path) -> None:
    env = _install_env(tmp_path)
    first = _run_installer(_INSTALL_SH, [], env, {"VERSION": "nonsense"})
    second = _run_installer(_INSTALL_SH, [], env, {"VERSION": "nonsense"})
    first_ref = _block_field(_output(first), "Support reference")
    second_ref = _block_field(_output(second), "Support reference")
    assert first_ref != second_ref


# ---------------------------------------------------------------------------
# Log retention and the size cap.
# ---------------------------------------------------------------------------


@pytest.mark.posix_only
def test_only_the_newest_five_logs_are_kept(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    state.mkdir(parents=True)
    for day in range(1, 7):
        seeded = state / f"install-2026010{day}T000000Z-aaaaaaaa.log"
        seeded.write_text("old transcript\n", encoding="utf-8")
        stamp = 1767225600 + day * 86400
        os.utime(seeded, (stamp, stamp))

    result = _run_installer(_INSTALL_SH, [], _install_env(tmp_path), {"VERSION": "nonsense"})
    assert result.returncode == 1

    remaining = [path.name for path in _logs(tmp_path)]
    assert len(remaining) == 5
    assert "install-20260101T000000Z-aaaaaaaa.log" not in remaining
    assert "install-20260102T000000Z-aaaaaaaa.log" not in remaining
    assert "install-20260106T000000Z-aaaaaaaa.log" in remaining
    new_log = Path(_block_field(_output(result), "Install log")).name
    assert new_log in remaining


@pytest.mark.posix_only
def test_oversized_transcript_is_capped_and_keeps_the_tail(tmp_path: Path) -> None:
    """The cap must keep the END of the transcript — that is where the failure is."""
    source = _INSTALL_SH.read_text(encoding="utf-8")
    patched_source = source.replace("MAX_LOG_BYTES=1048576", "MAX_LOG_BYTES=1024")
    assert patched_source != source, "install.sh no longer declares MAX_LOG_BYTES=1048576"

    patched = tmp_path / "install-capped.sh"
    patched.write_text(patched_source, encoding="utf-8")
    patched.chmod(0o755)

    result = _run_installer(patched, ["--unknown-option"], _install_env(tmp_path))
    assert result.returncode == 1

    log_path = Path(_block_field(_output(result), "Install log"))
    transcript = log_path.read_text(encoding="utf-8")
    assert log_path.stat().st_size <= 1024
    assert transcript.startswith("[log truncated: this transcript is capped at 1024 bytes")
    assert "ERROR: unknown argument: --unknown-option" in transcript


# ---------------------------------------------------------------------------
# Structural guard: the block is emitted from the single EXIT trap, so a new
# failure path cannot silently miss it.
# ---------------------------------------------------------------------------


def test_support_block_is_emitted_from_the_single_exit_trap() -> None:
    source = _INSTALL_SH.read_text(encoding="utf-8")
    assert source.count("trap on_exit EXIT") == 1
    assert source.count("trap 'rm -rf") == 0, "a second EXIT trap would replace on_exit and drop the support block"
    assert source.count("print_support_block || true") == 1

    on_exit_body = source[source.index("on_exit() {") : source.index("trap on_exit EXIT")]
    assert "print_support_block" in on_exit_body
    assert 'if [[ "${rc}" -ne 0 ]]' in on_exit_body


# ---------------------------------------------------------------------------
# Redaction: run the installer's own helper over credential-shaped input.
# ---------------------------------------------------------------------------


def _redact(text: str) -> str:
    source = _INSTALL_SH.read_text(encoding="utf-8")
    start = source.index("redact() {")
    end = source.index("\n}\n", start) + len("\n}\n")
    helper = source[start:end]
    script = f'set -euo pipefail\n{helper}\nredact "$1"\n'
    result = subprocess.run(  # noqa: S603 - resolved bash, literal harness text
        [_bash(), "-c", script, "redact-harness", text],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize(
    ("secret_text", "leaked"),
    [
        ("https://user:s3cr3t@example.com/releases", "s3cr3t"),
        ("Authorization: Bearer abcdef123456", "abcdef123456"),
        ("token=abc123&other=1", "abc123"),
        ("https://host/x?X-Amz-Signature=deadbeefcafe", "deadbeefcafe"),
    ],
)
@pytest.mark.posix_only
def test_redaction_withholds_credential_shaped_values(secret_text: str, leaked: str) -> None:
    redacted = _redact(secret_text)
    assert "[redacted]" in redacted
    assert leaked not in redacted


@pytest.mark.posix_only
def test_redaction_leaves_public_urls_and_paths_untouched() -> None:
    public = "https://github.com/farnalabs/modulo/releases/download/bundle-v1.0.0/modulo-1.0.0-linux-amd64.tar.gz"
    assert _redact(public) == public
    log_path = "/home/operator/.local/state/modulo/install-20261102T101500Z-a1b2c3d4.log"
    assert _redact(log_path) == log_path
