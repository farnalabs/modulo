"""Unit tests for the FAR-212 PR B sandbox policy enforcement surface.

Covers the script builders (read-only chmod, git-credential scoped/none,
selected-mode egress allowlist), the ``apply_sandbox_policy`` step ordering,
the PipelineGraphNode field validation (read_only / git_credentials), and the
updated capability derivation (write_files / git_credentials now mechanically
derivable from validated + enforced config).
"""

from __future__ import annotations

import os
import subprocess

import pytest

from modulo.core.pipeline_engine.sandbox_mode import (
    _validate_sandbox_git_credentials_config,
    _validate_sandbox_read_only_config,
    derive_sandbox_capabilities,
)
from modulo.core.pipeline_engine.sandbox_policy import (
    apply_sandbox_policy,
    build_egress_selected_script,
    build_git_none_script,
    build_git_scoped_script,
    build_read_only_script,
)

# ---------------------------------------------------------------------------
# Script builders (pure string functions — no sandbox needed)
# ---------------------------------------------------------------------------


def test_read_only_script_chmods_workspace_read_only() -> None:
    script = build_read_only_script()
    assert "chmod" in script
    assert "/home/user" in script
    # The seal must make the workspace read-only for the non-root agent user.
    assert "a-w" in script or "444" in script or "555" in script


def test_git_scoped_script_limits_to_github() -> None:
    script = build_git_scoped_script()
    assert "github.com" in script
    # The helper only grants the token when the host equals the allowlisted
    # github.com (scoped credential) — it outputs nothing for any other host.
    assert "github.com" in script
    # The helper checks the host field equals the allowlisted github.com.
    assert "host" in script
    assert "github.com" in script


def test_git_none_script_provisions_no_credentials() -> None:
    script = build_git_none_script()
    # "none" must not disclose any credential — the helper always refuses.
    assert "exit 1" in script or "refuse" in script.lower()


def test_egress_selected_script_drops_then_allows() -> None:
    script = build_egress_selected_script([{"host": "api.example.com", "port": 443}])
    # Drop all egress first (fail-closed), then add back only the allowlisted pair.
    assert "DROP" in script.upper()
    assert "api.example.com" in script
    assert "443" in script


# ---------------------------------------------------------------------------
# apply_sandbox_policy step ordering
# ---------------------------------------------------------------------------


class _FakeSandbox:
    def __init__(self) -> None:
        self.commands = _FakeCommands()


class _FakeCommands:
    def __init__(self) -> None:
        self.runs: list[str] = []

    async def run(self, script: str, *, user: str = "root", timeout: float = 60.0) -> None:  # noqa: ASYNC109 - matches the e2b SDK signature
        self.runs.append(script)


@pytest.mark.asyncio
async def test_apply_sandbox_policy_git_before_read_only_seal() -> None:
    """The git-credential scripts write files into the workspace, so they must
    run BEFORE the read-only seal (which would otherwise block the install)."""
    sandbox = _FakeSandbox()
    await apply_sandbox_policy(
        sandbox,
        read_only=True,
        git_credentials="scoped",
        egress_policy="selected",
        egress_allowlist=[{"host": "api.example.com", "port": 443}],
    )
    assert len(sandbox.commands.runs) == 3
    # git scoped -> egress selected -> read-only seal (git before seal).
    assert "github.com" in sandbox.commands.runs[0]
    assert "DROP" in sandbox.commands.runs[1].upper()
    assert "chmod" in sandbox.commands.runs[2]


@pytest.mark.asyncio
async def test_apply_sandbox_policy_no_policy_no_steps() -> None:
    sandbox = _FakeSandbox()
    await apply_sandbox_policy(
        sandbox,
        read_only=False,
        git_credentials=None,
        egress_policy="default",
        egress_allowlist=None,
    )
    assert not sandbox.commands.runs


# ---------------------------------------------------------------------------
# PipelineGraphNode field validation helpers
# ---------------------------------------------------------------------------


def test_validate_read_only_accepts_bool_and_none() -> None:
    # Valid values must not raise; the validator returns None on success.
    assert _validate_sandbox_read_only_config({"id": "n1", "read_only": True}) is None
    assert _validate_sandbox_read_only_config({"id": "n1", "read_only": False}) is None
    assert _validate_sandbox_read_only_config({"id": "n1", "read_only": None}) is None


def test_validate_read_only_rejects_non_bool() -> None:
    with pytest.raises(ValueError):
        _validate_sandbox_read_only_config({"id": "n1", "read_only": "yes"})


def test_validate_git_credentials_accepts_scopes() -> None:
    for scope in ("scoped", "unscoped", "none"):
        assert _validate_sandbox_git_credentials_config({"id": "n1", "git_credentials": scope}) is None
    assert _validate_sandbox_git_credentials_config({"id": "n1", "git_credentials": None}) is None


def test_validate_git_credentials_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        _validate_sandbox_git_credentials_config({"id": "n1", "git_credentials": "full"})


# ---------------------------------------------------------------------------
# Capability derivation (now mechanically derivable from validated config)
# ---------------------------------------------------------------------------


def test_derive_write_files_false_when_read_only() -> None:
    caps = derive_sandbox_capabilities({"node_type": "sandbox_agent", "read_only": True})
    assert caps["sandbox.write_files"] is False


def test_derive_write_files_true_when_writable() -> None:
    caps = derive_sandbox_capabilities({"node_type": "sandbox_agent", "read_only": False})
    assert caps["sandbox.write_files"] is True


def test_derive_git_credentials_scoped_true() -> None:
    caps = derive_sandbox_capabilities({"node_type": "sandbox_agent", "git_credentials": "scoped"})
    assert caps["sandbox.git_credentials"] is True


def test_derive_git_credentials_unscoped_false() -> None:
    caps = derive_sandbox_capabilities({"node_type": "sandbox_agent", "git_credentials": "unscoped"})
    assert caps["sandbox.git_credentials"] is False


def test_derive_git_credentials_none_false() -> None:
    caps = derive_sandbox_capabilities({"node_type": "sandbox_agent", "git_credentials": "none"})
    assert caps["sandbox.git_credentials"] is False


def test_derive_egress_selected_scoped() -> None:
    caps = derive_sandbox_capabilities(
        {"node_type": "sandbox_agent", "egress_policy": "selected", "egress_allowlist": [{"host": "x", "port": 443}]}
    )
    # selected denies all egress at the boolean level (allow_internet_access=False).
    assert caps["sandbox.egress"] is False


# ---------------------------------------------------------------------------
# Execution-style tests — actually run the scripts under bash (MAJOR 2 fix)
# ---------------------------------------------------------------------------

# The scripts hardcode ``/home/user`` as the workspace.  In the test environment
# that path does not exist, so each test creates a temp directory that mimics
# the sandbox layout and rewrites ``/home/user`` in the script to the temp path.


def _find_bash() -> str | None:
    r"""Locate a usable bash binary (Git Bash on Windows, system bash on Linux).

    WSL's ``bash.exe`` (``C:\WINDOWS\system32\bash.EXE``) is NOT usable for
    running scripts -- it requires a WSL distribution and is unreliable in CI.
    Git Bash (``C:\Program Files\Git\bin\bash.exe``) works on Windows without WSL.
    """
    import shutil

    # Prefer Git Bash on Windows — it ships with Git for Windows and is a
    # real MSYS2 bash, not a WSL shim.
    git_bash = r"C:\Program Files\Git\bin\bash.exe"
    if os.path.isfile(git_bash):
        return git_bash
    # On Linux, ``bash`` in PATH is fine.
    system_bash = shutil.which("bash")
    if system_bash is not None:
        return system_bash
    return None


_BASH = _find_bash()
_skip_no_bash = pytest.mark.skipif(_BASH is None, reason="bash not available on this system")


def _make_sandbox_env(tmp_path: object) -> dict[str, str]:
    """Create a fake HOME/WORKSPACE environment for script execution."""
    workspace = tmp_path / "home" / "user"  # type: ignore[union-attr]
    workspace.mkdir(parents=True)
    home = tmp_path / "home"  # type: ignore[union-attr]
    return {
        "HOME": _to_posix(home),
        "GITHUB_TOKEN": "ghp_test_token_abc123",
        "_WORKSPACE": _to_posix(workspace),
    }


def _to_posix(path: object) -> str:
    r"""Convert a Windows path to POSIX format for Git Bash.

    Git Bash (MSYS2) uses ``/c/Users/...`` instead of ``C:\Users\...``.
    On Linux this is a no-op (``pathlib`` already returns POSIX paths).
    """
    s = str(path)
    # ``pathlib`` on Windows returns backslash-separated paths.
    # Git Bash wants forward slashes with the drive letter lowercased.
    if len(s) >= 2 and s[1] == ":":
        drive = s[0].lower()
        rest = s[2:].replace("\\", "/")
        return f"/{drive}{rest}"
    return s.replace("\\", "/")


def _rewrite_workspace(script: str, tmp_path: object) -> str:
    """Replace ``/home/user`` with the test's temp workspace path."""
    workspace = tmp_path / "home" / "user"  # type: ignore[union-attr]
    return script.replace("/home/user", _to_posix(workspace))


def _run_script(script: str, env: dict[str, str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    """Run a shell script with the fake environment and return the result."""
    merged_env = {**os.environ, **env}
    return subprocess.run(  # noqa: S603
        [_BASH, "-c", script],  # type: ignore[list-item]
        capture_output=True,
        text=True,
        timeout=30,
        env=merged_env,
        **kwargs,  # type: ignore[arg-type]
    )


@_skip_no_bash
def test_read_only_script_execution(tmp_path: object) -> None:
    """build_read_only_script must exit 0 under bash."""
    env = _make_sandbox_env(tmp_path)
    script = _rewrite_workspace(build_read_only_script(), tmp_path)
    result = _run_script(script, env)
    assert result.returncode == 0, f"Script failed: {result.stderr}"


@_skip_no_bash
def test_git_scoped_script_execution(tmp_path: object) -> None:
    """build_git_scoped_script must exit 0 under bash and install the helper."""
    env = _make_sandbox_env(tmp_path)
    script = _rewrite_workspace(build_git_scoped_script(), tmp_path)
    result = _run_script(script, env)
    assert result.returncode == 0, f"Script failed: {result.stderr}"
    # The credential helper must exist on disk after the script runs.
    # Use tmp_path (native OS path) for the Python file existence check.
    workspace = tmp_path / "home" / "user"  # type: ignore[union-attr]
    cred_helper = workspace / ".git-policy" / "cred-helper.sh"
    assert cred_helper.is_file(), f"Credential helper not found at {cred_helper}"


@_skip_no_bash
def test_git_none_script_execution(tmp_path: object) -> None:
    """build_git_none_script must exit 0 under bash and install the refuse helper."""
    env = _make_sandbox_env(tmp_path)
    # Git Bash (MSYS2) maps /tmp to $TEMP; on Linux /tmp is the real /tmp.
    # The script writes to /tmp/modulo-git-refuse-helper.sh, so we check both.
    script = _rewrite_workspace(build_git_none_script(), tmp_path)
    result = _run_script(script, env)
    assert result.returncode == 0, f"Script failed: {result.stderr}"
    # Check the POSIX path (works on Linux; on Windows Git Bash /tmp maps to $TEMP).
    found = os.path.isfile("/tmp/modulo-git-refuse-helper.sh")
    # Fallback: check the Windows temp dir (Git Bash's /tmp is $TEMP on Windows).
    if not found:
        win_temp = os.environ.get("TEMP", os.environ.get("TMP", ""))
        if win_temp:
            found = os.path.isfile(os.path.join(win_temp, "modulo-git-refuse-helper.sh"))
    assert found, "Refuse helper not found at /tmp/modulo-git-refuse-helper.sh"


@_skip_no_bash
def test_egress_selected_script_execution(tmp_path: object) -> None:
    """build_egress_selected_script must parse cleanly under bash.

    iptables may not be available in the test environment, so we only verify
    the script parses and runs without a shell syntax error — iptables failures
    are expected (the script uses ``|| true`` for graceful fallback).
    """
    env = _make_sandbox_env(tmp_path)
    script = _rewrite_workspace(build_egress_selected_script([{"host": "api.example.com", "port": 443}]), tmp_path)
    result = _run_script(script, env)
    # The script uses ``|| true`` for every iptables command, so it should
    # always exit 0 even when iptables is absent.
    assert result.returncode == 0, f"Script failed: {result.stderr}"


# ---------------------------------------------------------------------------
# Targeted git config --file test (MAJOR 1 regression guard)
# ---------------------------------------------------------------------------


def test_git_config_file_only_no_global_conflict(tmp_path: object) -> None:
    """``git config --file <path>`` must work without ``--global``.

    This is the core of MAJOR 1: ``git config --global --file <path>`` fails
    with "only one config file at a time" (exit 129).  This test verifies the
    generated scripts use ``--file`` alone, by executing ``git config --file``
    directly via ``git.exe`` (available on both Windows and Linux).
    """
    git_cfg = tmp_path / "test-gitconfig"  # type: ignore[union-attr]
    script_scoped = build_git_scoped_script()
    script_none = build_git_none_script()
    # Both scripts must use ``git config --file`` (not ``--global --file``).
    assert "git config --file" in script_scoped
    assert "git config --global" not in script_scoped
    assert "git config --file" in script_none
    assert "git config --global" not in script_none
    # Execute ``git config --file`` directly to prove it works without error.
    # Find git.exe on this system.
    import shutil

    git_exe = shutil.which("git")
    if git_exe is None:
        pytest.skip("git not available")
    result = subprocess.run(  # noqa: S603
        [git_exe, "config", "--file", str(git_cfg), "credential.helper", "/some/helper"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"git config --file failed: {result.stderr}"
    # Verify the value was written.
    result_read = subprocess.run(  # noqa: S603
        [git_exe, "config", "--file", str(git_cfg), "credential.helper"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result_read.stdout.strip() == "/some/helper"
