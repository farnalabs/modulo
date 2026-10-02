"""Unit tests for the FAR-212 PR B sandbox policy enforcement surface.

Covers the script builders (read-only chmod, git-credential scoped/none,
selected-mode egress allowlist), the ``apply_sandbox_policy`` step ordering,
the PipelineGraphNode field validation (read_only / git_credentials), and the
updated capability derivation (write_files / git_credentials now mechanically
derivable from validated + enforced config).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from modulo.core.pipeline_engine.sandbox_mode import (
    _validate_sandbox_git_credentials_config,
    _validate_sandbox_read_only_config,
    derive_sandbox_capabilities,
)
from modulo.core.pipeline_engine.sandbox_policy import (
    _GH_PR_GUARD_CLAIM_RECEIPT,
    _GH_PR_GUARD_CLAIM_SENTINEL,
    _GH_PR_GUARD_FINGERPRINT,
    acquire_run_pr_guard,
    apply_sandbox_policy,
    build_egress_selected_script,
    build_gh_pr_guard_script,
    build_git_none_script,
    build_git_scoped_script,
    build_read_only_script,
    gh_pr_claim_receipt_path,
    gh_pr_guard_marker_path,
    harvest_gh_pr_claim_bounded,
    harvest_gh_pr_claim_via_exec,
    install_gh_pr_guard_via_exec,
    reset_run_pr_guard_claims,
    settle_run_pr_guard,
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
    assert "host" in script
    # FAR-212 PR B review (MAJOR 1): the helper must be registered in the AGENT's
    # git config (/home/user/.gitconfig), the file the agent's non-root user
    # actually reads — never /root/.gitconfig. A root-only registration would
    # silently no-op and leave the scoped credential unenforced (fail-open).
    assert "/home/user/.gitconfig" in script
    assert "credential.helper" in script


def test_git_none_script_provisions_no_credentials() -> None:
    script = build_git_none_script()
    # "none" must not disclose any credential — the helper always refuses.
    assert "exit 1" in script or "refuse" in script.lower()
    # Like the scoped script, the refuse helper is registered in the AGENT's
    # git config so it binds the agent's git, not a root config it never reads.
    assert "/home/user/.gitconfig" in script


def test_egress_selected_script_drops_then_allows() -> None:
    script = build_egress_selected_script([{"host": "api.example.com", "port": 443}])
    # Drop all egress first (fail-closed), then add back only the allowlisted pair.
    assert "DROP" in script.upper()
    assert "api.example.com" in script
    assert "443" in script


# ---------------------------------------------------------------------------
# Execution tests (FAR-212 PR B review, MAJOR 2): the enforcement scripts are
# security-critical, so they must not just CONTAIN the right strings — they
# must actually EXIT 0 when run. The previous string+step-order tests passed
# even though `git config --global --file` fails at runtime (MAJOR 1, exit 129
# "only one config file at a time"), which broke every scoped/none sandbox.
# These tests render each script, substitute the hardcoded /home/user workspace
# for an isolated temp dir, execute it under `sh`, and assert exit 0 + the
# git credential helper actually installs.
# ---------------------------------------------------------------------------

_WORKSPACE_SENTINEL = "/home/user"


def _render_for_temp_workspace(script: str, workspace: str) -> str:
    """Return the script with the hardcoded /home/user workspace swapped for a
    temp dir so executing it does not touch the real filesystem."""
    return script.replace(_WORKSPACE_SENTINEL, workspace)


def _run_script(script: str, workspace: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    r"""Execute a policy script under sh in an isolated temp workspace.

    HOME and GIT_CONFIG are pointed at the temp workspace so git writes the
    agent gitconfig there (mirroring the real non-root agent reading
    /home/user/.gitconfig), and GIT_CONFIG_NOSYSTEM pins an empty system config.

    On Windows, Git Bash (``C:\Program Files\Git\bin\bash.exe``) is used instead
    of ``sh`` (which is not available).  The workspace path is converted to
    POSIX format for Git Bash compatibility.
    """
    rerendered = _render_for_temp_workspace(script, workspace)
    run_env = {
        "HOME": workspace,
        "GIT_CONFIG_NOSYSTEM": "1",
        "PATH": os.environ["PATH"],
    }
    if env:
        run_env.update(env)

    # On Windows, use Git Bash instead of sh (sh is not available natively).
    shell_cmd: list[str]
    if os.name == "nt":
        git_bash = r"C:\Program Files\Git\bin\bash.exe"
        if not Path(git_bash).is_file():
            pytest.skip("Git Bash not available on this Windows system")
        # Convert workspace path to POSIX for Git Bash (e.g. C:\Users\... → /c/Users/...).
        posix_workspace = workspace
        if len(workspace) >= 2 and workspace[1] == ":":
            drive = workspace[0].lower()
            rest = workspace[2:].replace("\\", "/")
            posix_workspace = f"/{drive}{rest}"
        rerendered = _render_for_temp_workspace(script, posix_workspace)
        run_env["HOME"] = posix_workspace
        shell_cmd = [git_bash, "-c", rerendered]
    else:
        shell_cmd = ["sh", "-c", rerendered]
    return subprocess.run(  # noqa: S603 - executing our own generated policy script in tests
        shell_cmd,
        capture_output=True,
        text=True,
        env=run_env,
        cwd=workspace,
        timeout=60,
        check=False,
    )


def test_git_scoped_script_executes_and_installs_helper(tmp_path) -> None:
    """The scoped script must EXIT 0 and register the helper in the agent
    gitconfig (this catches the `--global --file` exit-129 regression)."""
    script = build_git_scoped_script()
    assert _WORKSPACE_SENTINEL in script
    result = _run_script(script, str(tmp_path))
    assert result.returncode == 0, f"scoped script failed: {result.stdout}\n{result.stderr}"
    gitconfig = tmp_path / ".gitconfig"
    assert gitconfig.exists(), "agent gitconfig not written"
    assert "cred-helper.sh" in gitconfig.read_text()
    helper = tmp_path / ".git-policy" / "cred-helper.sh"
    assert helper.exists()
    assert os.access(helper, os.X_OK)


@pytest.mark.skipif(os.name == "nt", reason="script writes to /tmp which is unreliable on Windows Git Bash")
def test_git_none_script_executes_and_installs_refuse_helper(tmp_path) -> None:
    """The 'none' script must EXIT 0 and register the refuse helper (also
    catches the `--global --file` exit-129 regression)."""
    script = build_git_none_script()
    result = _run_script(script, str(tmp_path))
    assert result.returncode == 0, f"none script failed: {result.stdout}\n{result.stderr}"
    gitconfig = tmp_path / ".gitconfig"
    assert gitconfig.exists(), "agent gitconfig not written"
    assert "modulo-git-refuse-helper.sh" in gitconfig.read_text()
    # On Linux the refuse helper lands at /tmp/modulo-git-refuse-helper.sh.
    # On Windows Git Bash, /tmp maps to $TEMP, so check both locations.
    if os.name != "nt":
        assert Path("/tmp/modulo-git-refuse-helper.sh").is_file()
    else:
        win_temp = os.environ.get("TEMP", os.environ.get("TMP", ""))
        if win_temp:
            assert Path(win_temp, "modulo-git-refuse-helper.sh").is_file()


def test_read_only_script_executes_clearly(tmp_path) -> None:
    """The read-only seal must EXIT 0 (chmod + re-open runtime writes)."""
    script = build_read_only_script()
    result = _run_script(script, str(tmp_path))
    assert result.returncode == 0, f"read-only script failed: {result.stdout}\n{result.stderr}"


@pytest.mark.skipif(os.name == "nt", reason="Git Bash stdin handling differs from sh")
def test_scoped_helper_grants_token_only_to_allowed_host() -> None:
    """Executing the credential helper itself: it echoes the token for
    github.com and nothing for any other host (executes the real sh snippet)."""
    from modulo.core.pipeline_engine.sandbox_policy import _credential_helper_script

    helper = _credential_helper_script()
    token = "ghp_testtoken123"
    # Use Git Bash on Windows, sh on Linux.
    sh_cmd = ["sh", "-c", helper]
    if os.name == "nt":
        git_bash = r"C:\Program Files\Git\bin\bash.exe"
        if Path(git_bash).is_file():
            sh_cmd = [git_bash, "-c", helper]
    allowed = subprocess.run(  # noqa: S603 - executing our own helper script in tests
        sh_cmd,
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
        env={"GITHUB_TOKEN": token, "PATH": os.environ["PATH"]},
        timeout=60,
        check=False,
    )
    assert allowed.returncode == 0
    assert f"password={token}" in allowed.stdout
    denied = subprocess.run(  # noqa: S603 - executing our own helper script in tests
        sh_cmd,
        input="protocol=https\nhost=gitlab.com\n\n",
        capture_output=True,
        text=True,
        env={"GITHUB_TOKEN": token, "PATH": os.environ["PATH"]},
        timeout=60,
        check=False,
    )
    assert denied.returncode == 0
    assert "password=" not in denied.stdout


# ---------------------------------------------------------------------------
# apply_sandbox_policy step ordering
# ---------------------------------------------------------------------------


class _FakeSandbox:
    def __init__(self, fail_on: set[int] | None = None) -> None:
        self.commands = _FakeCommands(fail_on=fail_on)


class _FakeCommands:
    def __init__(self, fail_on: set[int] | None = None) -> None:
        self.runs: list[str] = []
        self._fail_on = fail_on or set()
        self._call_count = 0

    async def run(self, script: str, *, user: str = "root", timeout: float = 60.0) -> None:  # noqa: ASYNC109 - matches the e2b SDK signature
        call = self._call_count
        self._call_count += 1
        if call in self._fail_on:
            raise RuntimeError(f"policy step failed: {call}")
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
# Failure semantics (FAR-212 PR B review, MAJOR 2): enforcement-critical steps
# (read_only seal + git-credential helper install) RAISE on failure so the run
# dispatches as a failure rather than silently certifying a deny-guarantee
# nothing enforced; the egress step (drop-first fail-closed) stays best-effort.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_sandbox_policy_read_only_failure_raises() -> None:
    """A failed read-only chmod must RAISE: the workspace would stay writable yet
    ``sandbox.write_files=False`` stays certified — fail-open if swallowed."""
    sandbox = _FakeSandbox(fail_on={0})
    with pytest.raises(RuntimeError):
        await apply_sandbox_policy(
            sandbox,
            read_only=True,
            git_credentials=None,
            egress_policy="default",
            egress_allowlist=None,
        )


@pytest.mark.asyncio
async def test_apply_sandbox_policy_git_scoped_failure_raises() -> None:
    """A failed git-helper install must RAISE: credentials would stay unscoped
    yet ``sandbox.git_credentials`` (scoped) stays certified — fail-open."""
    sandbox = _FakeSandbox(fail_on={0})
    with pytest.raises(RuntimeError):
        await apply_sandbox_policy(
            sandbox,
            read_only=False,
            git_credentials="scoped",
            egress_policy="default",
            egress_allowlist=None,
        )


@pytest.mark.asyncio
async def test_apply_sandbox_policy_egress_failure_is_best_effort() -> None:
    """A failed egress step is best-effort (logged-and-continued): its script is
    drop-first fail-closed, so it leaves deny-all — the safe direction. The
    follow-on read-only seal must still run."""
    sandbox = _FakeSandbox(fail_on={0})
    await apply_sandbox_policy(
        sandbox,
        read_only=True,
        git_credentials=None,
        egress_policy="selected",
        egress_allowlist=[{"host": "api.example.com", "port": 443}],
    )
    # egress failed (index 0, swallowed); read-only seal still ran (index 1).
    assert len(sandbox.commands.runs) == 1
    assert "chmod" in sandbox.commands.runs[0]


@pytest.mark.asyncio
async def test_apply_sandbox_policy_git_scoped_registers_agent_config() -> None:
    """The scoped git step must register the helper under the AGENT's git config
    file (/home/user/.gitconfig), never /root/.gitconfig — otherwise the
    non-root agent's git never honours the scoped credential (fail-open)."""
    sandbox = _FakeSandbox()
    await apply_sandbox_policy(
        sandbox,
        read_only=False,
        git_credentials="scoped",
        egress_policy="default",
        egress_allowlist=None,
    )
    assert "/home/user/.gitconfig" in sandbox.commands.runs[0]


@pytest.mark.asyncio
async def test_apply_sandbox_policy_multi_host_helper_reachable() -> None:
    """FAR-798 (blocking 1): when allowed_hosts is passed with scoped
    credentials, the multi-host helper is installed (not the single-host one),
    so the multi_host capability is genuinely enforced — the branch is
    reachable and not dead code."""
    sandbox = _FakeSandbox()
    await apply_sandbox_policy(
        sandbox,
        read_only=False,
        git_credentials="scoped",
        egress_policy="default",
        egress_allowlist=None,
        allowed_hosts={"github.com": "MODULO_GIT_CRED_0", "gitlab.com": "MODULO_GIT_CRED_1"},
    )
    # The installed helper must be the multi-host variant (both host case arms).
    assert "github.com" in sandbox.commands.runs[0]
    assert "gitlab.com" in sandbox.commands.runs[0]
    # Must not be the single-host scoped script (which only ever grants
    # github.com and never references a second host).
    assert "MODULO_GIT_CRED_1" in sandbox.commands.runs[0]


@pytest.mark.asyncio
async def test_apply_sandbox_policy_scoped_without_allowed_hosts_is_single_host() -> None:
    """FAR-798 (regression): scoped credentials without allowed_hosts still
    install the byte-identical single-host helper."""
    sandbox = _FakeSandbox()
    await apply_sandbox_policy(
        sandbox,
        read_only=False,
        git_credentials="scoped",
        egress_policy="default",
        egress_allowlist=None,
        allowed_hosts=None,
    )
    assert "github.com" in sandbox.commands.runs[0]
    assert "MODULO_GIT_CRED" not in sandbox.commands.runs[0]


@pytest.mark.asyncio
async def test_apply_sandbox_policy_multi_host_invalid_host_raises() -> None:
    """FAR-798 (review finding #3): a host containing shell-case metacharacters
    must fail-closed (raise) rather than emit an injectable helper."""
    sandbox = _FakeSandbox()
    with pytest.raises(ValueError, match="invalid git-credential host"):
        await apply_sandbox_policy(
            sandbox,
            read_only=False,
            git_credentials="scoped",
            egress_policy="default",
            egress_allowlist=None,
            allowed_hosts={"bad|host": "MODULO_GIT_CRED_0"},
        )


# ---------------------------------------------------------------------------
# PipelineGraphNode field validation helpers
# ---------------------------------------------------------------------------


def test_validate_read_only_accepts_bool_and_none() -> None:
    # Valid values must not raise; the validator returns None on success.
    assert _validate_sandbox_read_only_config({"id": "n1", "read_only": True}) is None
    assert _validate_sandbox_read_only_config({"id": "n1", "read_only": False}) is None
    assert _validate_sandbox_read_only_config({"id": "n1", "read_only": None}) is None


def test_validate_read_only_rejects_non_bool() -> None:
    with pytest.raises(ValueError, match="read_only must be a boolean"):
        _validate_sandbox_read_only_config({"id": "n1", "read_only": "yes"})


def test_validate_git_credentials_accepts_scopes() -> None:
    for scope in ("scoped", "unscoped", "none"):
        assert _validate_sandbox_git_credentials_config({"id": "n1", "git_credentials": scope}) is None
    assert _validate_sandbox_git_credentials_config({"id": "n1", "git_credentials": None}) is None


def test_validate_git_credentials_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="invalid git_credentials"):
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
# FAR-1264: run-scoped one-PR-per-run gh guard (marker, step wiring, and an
# end-to-end execution of the installed shim under sh)
# ---------------------------------------------------------------------------


def test_gh_pr_guard_marker_path_is_run_scoped() -> None:
    """Two runs get two distinct markers, so a marker can never leak a
    claimed state across runs even if a workspace outlived its run."""
    run_a = gh_pr_guard_marker_path("11111111-2222-3333-4444-555555555555")
    run_b = gh_pr_guard_marker_path("66666666-7777-8888-9999-000000000000")
    assert run_a != run_b
    assert run_a.startswith("/tmp/")
    assert "11111111-2222-3333-4444-555555555555" in run_a
    # No scope -> the stable fallback path (both None and "" mean "unscoped").
    assert gh_pr_guard_marker_path(None) == gh_pr_guard_marker_path("")


def test_gh_pr_guard_marker_path_sanitises_scope() -> None:
    """The scope is embedded in a filesystem path and single-quoted into a
    shell script — quotes, spaces and metacharacters must never survive."""
    marker = gh_pr_guard_marker_path("run'$(evil)`; drop --")
    for dangerous in ("'", '"', "`", "$", ";", "(", ")", " ", "|"):
        assert dangerous not in marker, f"dangerous char {dangerous!r} survived sanitisation: {marker}"
    assert marker.startswith("/tmp/")
    assert marker.endswith(".marker")


def test_gh_pr_guard_marker_path_falls_back_when_scope_sanitises_to_empty() -> None:
    """A non-empty scope made entirely of characters the sanitiser rewrites to
    ``_`` (``///`` -> ``___``) or strips (``...``) reduces to nothing: the path
    must fall back to the stable unscoped marker, never emit a bare prefix."""
    fallback = gh_pr_guard_marker_path(None)
    assert gh_pr_guard_marker_path("///") == fallback
    assert gh_pr_guard_marker_path("...") == fallback


def test_gh_pr_guard_script_preserves_real_gh_and_embeds_marker() -> None:
    """The install script must resolve the real gh BEFORE shadowing (no
    self-recursion) and embed the run-scoped marker path."""
    marker = gh_pr_guard_marker_path("scope-1")
    script = build_gh_pr_guard_script(marker)
    assert marker in script
    # The real gh is copied aside to <path>.modulo-real before the shim
    # replaces its path — the shim execs that, never itself (the script
    # builds the name as ``$tgt.modulo-real`` with ``$tgt="$d/gh"``).
    assert ".modulo-real" in script
    assert "one-PR-per-run guard" in script
    assert "mkdir" in script  # atomic marker claim
    # Best-effort contract: a guarded failure must not abort the whole step
    # silently, and an absent gh is "nothing to guard", not an error.
    assert "cannot preserve real gh" in script
    assert "no gh on PATH" in script


@pytest.mark.asyncio
async def test_apply_sandbox_policy_flag_only_runs_the_guard_step() -> None:
    """FAR-1273: a node with single_pr_per_run=True and every enforcement
    control default (Prompt-to-PR's shape) runs EXACTLY one step: the
    gh-guard install."""
    sandbox = _FakeSandbox()
    await apply_sandbox_policy(
        sandbox,
        read_only=False,
        git_credentials=None,
        egress_policy=None,
        egress_allowlist=None,
        single_pr_per_run=True,
        run_scope="run-abc",
    )
    assert len(sandbox.commands.runs) == 1
    assert ".modulo-real" in sandbox.commands.runs[0]
    assert "run-abc" in sandbox.commands.runs[0]


@pytest.mark.asyncio
async def test_apply_sandbox_policy_without_the_flag_runs_no_guard_step() -> None:
    """Regression: a node WITHOUT the single_pr_per_run flag gets the
    byte-identical pre-FAR-1264 step list — no guard step appears."""
    sandbox = _FakeSandbox()
    await apply_sandbox_policy(
        sandbox,
        read_only=True,
        git_credentials="scoped",
        egress_policy="selected",
        egress_allowlist=[{"host": "api.example.com", "port": 443}],
    )
    assert len(sandbox.commands.runs) == 3
    assert all(".modulo-real" not in script for script in sandbox.commands.runs)


@pytest.mark.asyncio
async def test_apply_sandbox_policy_guard_step_runs_before_read_only_seal() -> None:
    """The guard install writes (system PATH dirs + /tmp), so it must run
    BEFORE the read-only seal; git-credential steps stay first (unchanged)."""
    sandbox = _FakeSandbox()
    await apply_sandbox_policy(
        sandbox,
        read_only=True,
        git_credentials="scoped",
        egress_policy=None,
        egress_allowlist=None,
        single_pr_per_run=True,
        run_scope="run-xyz",
    )
    # git scoped -> gh guard -> read-only seal.
    assert len(sandbox.commands.runs) == 3
    assert "github.com" in sandbox.commands.runs[0]
    assert ".modulo-real" in sandbox.commands.runs[1]
    assert "chmod" in sandbox.commands.runs[2]


@pytest.mark.asyncio
async def test_apply_sandbox_policy_guard_install_failure_is_best_effort() -> None:
    """A failed gh-guard install must NEVER raise (unlike the enforcement
    steps): the run proceeds exactly as before, degrading to the prompt-level
    one-PR-per-run guard. Follow-on steps still run."""
    # Guard-only node: the failing install is logged-and-continued, no raise.
    guard_only = _FakeSandbox(fail_on={0})
    await apply_sandbox_policy(
        guard_only,
        read_only=False,
        git_credentials=None,
        egress_policy=None,
        egress_allowlist=None,
        single_pr_per_run=True,
    )
    assert not guard_only.commands.runs

    # Guard + seal: the guard step fails at index 0, the seal still runs.
    guard_and_seal = _FakeSandbox(fail_on={0})
    await apply_sandbox_policy(
        guard_and_seal,
        read_only=True,
        git_credentials=None,
        egress_policy=None,
        egress_allowlist=None,
        single_pr_per_run=True,
    )
    assert len(guard_and_seal.commands.runs) == 1
    assert "chmod" in guard_and_seal.commands.runs[0]


@pytest.mark.asyncio
async def test_apply_sandbox_policy_guard_step_stderr_is_mirrored_into_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """XS: the "no gh on PATH / NOT platform-guarded" note exits 0 with only a
    stderr payload, which the step result would otherwise discard — an
    unguarded flagged run must be observable in the policy log."""

    class _StderrCommands:
        async def run(self, script: str, *, user: str = "root", timeout: float = 60.0) -> SimpleNamespace:  # noqa: ASYNC109 - matches the e2b SDK signature
            return SimpleNamespace(
                stderr="modulo: gh guard: WARNING no gh on PATH; nothing to guard", stdout="", exit_code=0
            )

    class _StderrSandbox:
        commands = _StderrCommands()

    with caplog.at_level(logging.WARNING, logger="modulo.core.pipeline_engine.sandbox_policy"):
        await apply_sandbox_policy(
            _StderrSandbox(),
            read_only=False,
            git_credentials=None,
            egress_policy=None,
            egress_allowlist=None,
            single_pr_per_run=True,
            run_scope="run-log",
        )
    assert "gh_guard_install_reported" in caplog.text
    assert "no gh on PATH" in caplog.text


# --- End-to-end shim execution (the guard must actually DO it) -------------


def _posixify(path: str) -> str:
    """C:\\a\\b -> /c/a/b for Git Bash; a no-op on POSIX."""
    if os.name != "nt":
        return path
    return "/" + path[0].lower() + path[2:].replace("\\", "/")


def _sh_runner_argv() -> list[str]:
    if os.name == "nt":
        git_bash = r"C:\Program Files\Git\bin\bash.exe"
        if not Path(git_bash).is_file():
            pytest.skip("Git Bash not available on this Windows system")
        return [git_bash]
    return ["sh"]


def _run_in_sh(body: str, *, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Write *body* as an LF shell file and execute it.

    The script is passed as a FILE (never embedded in a -c argument) so
    Windows argument quoting can never mangle the embedded heredocs/quotes,
    and the PATH the scripts see is set INSIDE the script in POSIX form —
    MSYS's Windows->posix PATH conversion can then not mangle it either.
    """
    runner = cwd / f"_runner_{uuid.uuid4().hex}.sh"
    runner.write_text(body, encoding="utf-8", newline="\n")
    return subprocess.run(  # noqa: S603 - executing our own generated scripts
        [*_sh_runner_argv(), str(runner)],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        # M5: the guard script itself runs in well under a second, but a
        # spawn/AV-IO stall on a loaded Windows box once pushed a run past 60s
        # (TimeoutExpired, three runs). Generous bound: a timeout here is an
        # environment stall, never a signal about the script's behaviour — the
        # assertions below are the actual verdict.
        timeout=240,
        check=False,
    )


def _fake_gh(bindir: Path, *, fail_next: bool = False) -> Path:
    """A stand-in real gh: appends its args to <its dir>/gh-calls.log.

    With ``fail_next=True`` it exits 1 on its FIRST invocation (consuming an
    arming file) and succeeds afterwards — the M1 shape: a transient first
    ``gh pr create`` failure (rate limit, empty diff, network flap) that must
    NOT burn the run's only attempt. Without the arm file its behaviour is the
    unchanged exit-0 baseline every other test relies on.
    """
    bindir.mkdir(parents=True, exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        '_d="$(dirname "$0")"\n'
        'printf \'%s\\n\' "$*" >> "$_d/gh-calls.log"\n'
        'if [ -f "$_d/gh-fail-arm" ]; then rm -f "$_d/gh-fail-arm"; exit 1; fi\n'
        "exit 0\n",
        encoding="utf-8",
        newline="\n",
    )
    gh.chmod(0o700)
    if fail_next:
        (bindir / "gh-fail-arm").write_text("", encoding="utf-8")
    return gh


def _shim_text(bindir: Path) -> str:
    """The INSTALLED ``gh`` path's contents (the shim, once guarded)."""
    return (bindir / "gh").read_text(encoding="utf-8")


def _install_guard(bindir: Path, workdir: Path, marker: str, *, pre_spent: bool = False) -> None:
    posix_bin = _posixify(str(bindir))
    install = workdir / "install.sh"
    install.write_text(build_gh_pr_guard_script(marker, pre_spent=pre_spent), encoding="utf-8", newline="\n")
    body = f"PATH={posix_bin}:/usr/bin:/bin\nexport PATH\nsh '{_posixify(str(install))}'\n"
    result = _run_in_sh(body, cwd=workdir)
    assert result.returncode == 0, f"guard install failed: {result.stdout}\n{result.stderr}"
    assert (bindir / "gh.modulo-real").is_file(), "the real gh must be preserved before its path is shadowed"
    assert "MODULO_GH_GUARD_EOF" not in result.stderr, "install heredoc must terminate cleanly"


def _gh(bindir: Path, workdir: Path, command: str) -> subprocess.CompletedProcess[str]:
    """Run *command* through ``sh -c '<command>'`` with ONLY the temp bindir
    ahead of the system PATH.

    This mirrors how node_runner executes the agent command
    (``["sh", "-c", wrapped_command]``), so PATH resolution genuinely reaches
    the shim — the mechanism the agent's bare ``gh`` invocations use.
    """
    posix_bin = _posixify(str(bindir))
    body = f"PATH={posix_bin}:/usr/bin:/bin\nexport PATH\nsh -c '{command}'\n"
    return _run_in_sh(body, cwd=workdir)


def _gh_calls(bindir: Path) -> list[str]:
    log = bindir / "gh-calls.log"
    if not log.is_file():
        return []
    return log.read_text(encoding="utf-8").splitlines()


def test_gh_pr_guard_first_create_passes_and_second_is_refused(tmp_path: Path) -> None:
    """The acceptance criterion, executed: the first ``gh pr create``
    delegates to the real gh unchanged (same args, same exit code); the
    second is refused WITHOUT the real gh being called."""
    bindir = tmp_path / "bin"
    _fake_gh(bindir)
    marker = gh_pr_guard_marker_path(f"pytest-{uuid.uuid4().hex}")
    _run_in_sh(f"rm -rf '{marker}'\n", cwd=tmp_path)
    _install_guard(bindir, tmp_path, marker)

    first = _gh(bindir, tmp_path, "gh pr create --title t")
    assert first.returncode == 0, f"first create must pass through: {first.stdout}\n{first.stderr}"

    second = _gh(bindir, tmp_path, "gh pr create --title t2")
    assert second.returncode != 0, "the second gh pr create must be refused with a non-zero exit"
    assert "one-PR-per-run guard" in second.stderr
    # M2: the refusal must be ACCURATE — it names what it refuses (a SECOND
    # attempt in this run) and states the fact that makes the marker meaningful
    # (an earlier create exited 0; with M1 only a successful create leaves a
    # marker), never the old unconditional "second gh pr create in this
    # sandbox run" wording that asserted a PR existed.
    assert "refusing a second 'gh pr create' attempt in this run" in second.stderr
    assert "exited 0" in second.stderr
    assert "refusing second gh pr create in this sandbox run" not in second.stderr
    # Exactly ONE real-gh call, carrying the FIRST invocation's args verbatim.
    assert _gh_calls(bindir) == ["pr create --title t"]
    # The claim marker exists (checked through sh: on Windows Git Bash's /tmp
    # is not Python's /tmp).
    claim = _run_in_sh(f"test -d '{marker}' && echo claimed\n", cwd=tmp_path)
    assert claim.stdout.strip() == "claimed"
    _run_in_sh(f"rm -rf '{marker}'\n", cwd=tmp_path)


def test_gh_pr_guard_failed_first_create_releases_the_claim_for_a_retry(tmp_path: Path) -> None:
    """M1: a FAILED first create must not burn the run's only attempt.

    The shim claims with ``mkdir`` but holds the claim only while the real gh
    is running: a non-zero exit releases it (``rmdir``) and passes the code
    through, so the retry after a transient failure (rate limit, empty diff,
    network) is ALLOWED. A marker left behind by a failure would refuse that
    retry while reporting that a create had succeeded.
    """
    bindir = tmp_path / "bin"
    _fake_gh(bindir, fail_next=True)
    marker = gh_pr_guard_marker_path(f"pytest-{uuid.uuid4().hex}")
    _run_in_sh(f"rm -rf '{marker}'\n", cwd=tmp_path)
    _install_guard(bindir, tmp_path, marker)

    # First create: the real gh runs, fails, and its exit code passes through.
    first = _gh(bindir, tmp_path, "gh pr create --title t")
    assert first.returncode != 0, f"the real gh's failure must pass through: {first.stdout}\n{first.stderr}"
    # The claim was RELEASED — no marker survives a failed create.
    claim = _run_in_sh(f"test -d '{marker}' && echo claimed\n", cwd=tmp_path)
    assert not claim.stdout.strip(), f"a failed create must leave no claim marker: {claim.stdout}"

    # Second create (the retry): ALLOWED — this is the whole point of M1.
    second = _gh(bindir, tmp_path, "gh pr create --title t2")
    assert second.returncode == 0, f"a retry after a failed create must be allowed: {second.stdout}\n{second.stderr}"
    assert "one-PR-per-run guard" not in second.stderr

    # BOTH attempts reached the real gh (the failure did not consume the slot).
    assert _gh_calls(bindir) == ["pr create --title t", "pr create --title t2"]
    # The retry's SUCCESS now holds the claim, so a third attempt is refused.
    third = _gh(bindir, tmp_path, "gh pr create --title t3")
    assert third.returncode != 0
    assert "one-PR-per-run guard" in third.stderr
    assert _gh_calls(bindir) == ["pr create --title t", "pr create --title t2"]
    _run_in_sh(f"rm -rf '{marker}'\n", cwd=tmp_path)


# --- M4: install idempotency ("real preserved" != "guard installed") -------


def test_gh_pr_guard_reinstall_is_idempotent_for_the_same_run_scope(tmp_path: Path) -> None:
    """M4: re-running the install on an ALREADY-guarded workspace must SUCCEED.

    Under the old rule (``[ -f "$real" ] -> continue``) a second install found
    the preserved real gh, skipped the only directory, guarded nothing, and
    then reported "a gh exists but none could be guarded" (exit 1) — a false
    failure on every reused workspace. A scope-matching shim now COUNTS as
    guarded.
    """
    bindir = tmp_path / "bin"
    _fake_gh(bindir)
    marker = gh_pr_guard_marker_path(f"pytest-{uuid.uuid4().hex}")
    _run_in_sh(f"rm -rf '{marker}'\n", cwd=tmp_path)
    _install_guard(bindir, tmp_path, marker)
    # _install_guard asserts exit 0 + the preserved real gh; a second run of
    # the SAME install script must hold both.
    _install_guard(bindir, tmp_path, marker)

    shim = _shim_text(bindir)
    assert _GH_PR_GUARD_FINGERPRINT in shim
    assert f"MARKER='{marker}'" in shim
    # The preserved copy is still the REAL gh, never a copy of the shim.
    real = (bindir / "gh.modulo-real").read_text(encoding="utf-8")
    assert _GH_PR_GUARD_FINGERPRINT not in real
    # And the guard still works after the double install.
    first = _gh(bindir, tmp_path, "gh pr create --title t")
    assert first.returncode == 0, first.stderr
    second = _gh(bindir, tmp_path, "gh pr create --title t2")
    assert second.returncode != 0
    assert "one-PR-per-run guard" in second.stderr
    _run_in_sh(f"rm -rf '{marker}'\n", cwd=tmp_path)


def test_gh_pr_guard_reinstall_rewrites_a_stale_run_scope(tmp_path: Path) -> None:
    """M4: a shim carrying an OLD run scope must be REWRITTEN on re-install.

    A stale-scope shim would claim a marker key to the previous run's scope —
    on a reused workspace that stale claim blocks every future run. Detecting
    the shim by its own fingerprint + embedded scope (not by the preserved
    ``gh.modulo-real``, which outlives it) is what makes the rewrite happen.
    """
    bindir = tmp_path / "bin"
    _fake_gh(bindir)
    stale = gh_pr_guard_marker_path("pytest-stale-scope")
    fresh = gh_pr_guard_marker_path(f"pytest-{uuid.uuid4().hex}")
    _run_in_sh(f"rm -rf '{stale}' '{fresh}'\n", cwd=tmp_path)
    _install_guard(bindir, tmp_path, stale)
    assert f"MARKER='{stale}'" in _shim_text(bindir)

    # Re-install with this run's scope: the stale shim is rewritten in place.
    _install_guard(bindir, tmp_path, fresh)
    shim = _shim_text(bindir)
    assert _GH_PR_GUARD_FINGERPRINT in shim, "the rewritten file must still be our shim"
    assert f"MARKER='{fresh}'" in shim, "the new run scope must be embedded"
    assert f"MARKER='{stale}'" not in shim, "the stale run scope must be gone"
    real = (bindir / "gh.modulo-real").read_text(encoding="utf-8")
    assert _GH_PR_GUARD_FINGERPRINT not in real, "the rewrite must never clobber the preserved real gh"

    # Functionally: the create claims THIS run's marker, not the stale one.
    first = _gh(bindir, tmp_path, "gh pr create --title t")
    assert first.returncode == 0, first.stderr
    fresh_claim = _run_in_sh(f"test -d '{fresh}' && echo claimed\n", cwd=tmp_path)
    assert fresh_claim.stdout.strip() == "claimed"
    stale_claim = _run_in_sh(f"test -d '{stale}' && echo claimed\n", cwd=tmp_path)
    assert not stale_claim.stdout.strip()
    _run_in_sh(f"rm -rf '{stale}' '{fresh}'\n", cwd=tmp_path)


def test_gh_pr_guard_reinstall_repairs_a_preserved_but_unguarded_gh(tmp_path: Path) -> None:
    """M4: ``gh.modulo-real`` present but ``gh`` NOT a shim (partial install)
    must be RE-PAIRED and reported as success, not as "none could be guarded"."""
    bindir = tmp_path / "bin"
    _fake_gh(bindir)
    # Partial state: the real gh was preserved but the shadow never happened.
    # Mirror the install's ``cp -p`` (which preserves the executable bit) — a
    # mode-stripped copy would make the shim's ``exec`` fail with EACCES on
    # Linux, which is not the partial state this test models.
    shutil.copy2(bindir / "gh", bindir / "gh.modulo-real")
    marker = gh_pr_guard_marker_path(f"pytest-{uuid.uuid4().hex}")
    _run_in_sh(f"rm -rf '{marker}'\n", cwd=tmp_path)
    # _install_guard asserts exit 0 — the old rule exited 1 here.
    _install_guard(bindir, tmp_path, marker)

    shim = _shim_text(bindir)
    assert _GH_PR_GUARD_FINGERPRINT in shim
    assert f"MARKER='{marker}'" in shim
    first = _gh(bindir, tmp_path, "gh pr create --title t")
    assert first.returncode == 0, first.stderr
    second = _gh(bindir, tmp_path, "gh pr create --title t2")
    assert second.returncode != 0
    assert "one-PR-per-run guard" in second.stderr
    _run_in_sh(f"rm -rf '{marker}'\n", cwd=tmp_path)


def test_gh_pr_guard_detects_flag_prefixed_create_and_passes_others_through(tmp_path: Path) -> None:
    """``gh --repo X pr create`` counts as the run's one create; every other
    subcommand (and ``gh issue create``) passes through untouched."""
    bindir = tmp_path / "bin"
    _fake_gh(bindir)
    marker = gh_pr_guard_marker_path(f"pytest-{uuid.uuid4().hex}")
    _run_in_sh(f"rm -rf '{marker}'\n", cwd=tmp_path)
    _install_guard(bindir, tmp_path, marker)

    # Flag-prefixed create is DETECTED: it claims the marker...
    flagged = _gh(bindir, tmp_path, "gh --repo o/r pr create --title t")
    assert flagged.returncode == 0, flagged.stderr
    # ...so a later plain create is refused (proving the first was counted).
    plain = _gh(bindir, tmp_path, "gh pr create --title t2")
    assert plain.returncode != 0
    assert "one-PR-per-run guard" in plain.stderr

    # Every other gh invocation passes straight through, in the same run.
    for command in (
        "gh pr list",
        "gh pr status",
        "gh pr list --json create",
        "gh issue create --title x",
        "gh --version",
    ):
        result = _gh(bindir, tmp_path, command)
        assert result.returncode == 0, f"{command!r} must pass through untouched: {result.stderr}"

    assert _gh_calls(bindir) == [
        "--repo o/r pr create --title t",
        "pr list",
        "pr status",
        "pr list --json create",
        "issue create --title x",
        "--version",
    ]
    _run_in_sh(f"rm -rf '{marker}'\n", cwd=tmp_path)


# --- FAR-1315: run-scoped claim across MULTIPLE flagged nodes ---------------
#
# The FAR-1264 marker lives inside ONE sandbox, so a pipeline with two flagged
# nodes had two independently claimable markers (one PR per node). These tests
# cover the platform-side run ledger that closes that gap: first node holds,
# concurrent/second nodes are denied, an observed claim spends the slot for the
# whole run, and a node that never claimed releases its hold.


@pytest.fixture(autouse=True)
def _reset_run_pr_guard_ledger():
    """Isolate the process-local run claim ledger between tests."""
    reset_run_pr_guard_claims()
    yield
    reset_run_pr_guard_claims()


def test_gh_pr_guard_shim_prints_claim_sentinel_on_a_successful_create(tmp_path: Path) -> None:
    """FAR-1315 observability: a SUCCESSFUL create emits the FULL fixed claim
    sentinel on stdout and writes the success RECEIPT; a refused create emits
    neither.

    The full literal (not a substring like ``RUN CLAIM ACQUIRED``, and not the
    imported constant) is asserted because ``settle_run_pr_guard`` matches the
    ENTIRE constant — a substring assert would pass while the shim's text
    drifted away from what settle looks for (a silent drift hole). Written as
    literal text rather than the constant so a shim that prints nothing (or
    something else) fails behaviourally. The receipt is the primary spend
    signal; the sentinel is its truncation-fallback twin."""
    bindir = tmp_path / "bin"
    _fake_gh(bindir)
    scope = f"pytest-{uuid.uuid4().hex}"
    marker = gh_pr_guard_marker_path(scope)
    receipt = gh_pr_claim_receipt_path(scope)
    _run_in_sh(f"rm -rf '{marker}' '{receipt}'\n", cwd=tmp_path)
    _install_guard(bindir, tmp_path, marker)

    first = _gh(bindir, tmp_path, "gh pr create --title t")
    assert first.returncode == 0, f"first create must pass through: {first.stdout}\n{first.stderr}"
    assert "modulo: one-PR-per-run guard: RUN CLAIM ACQUIRED" in first.stdout, (
        "a successful create must print the FULL FAR-1315 claim sentinel settle matches"
    )
    # The primary spend signal: a receipt file inside the marker dir, written
    # only on a successful create (survives stdout truncation; not exposed by
    # reading the shim text itself).
    claim = _run_in_sh(f"test -f '{receipt}' && echo receipt-present\n", cwd=tmp_path)
    assert claim.returncode == 0, f"a successful create must leave the claim receipt: {claim.stderr}"

    second = _gh(bindir, tmp_path, "gh pr create --title t2")
    assert second.returncode != 0, "the second create must be refused"
    assert "modulo: one-PR-per-run guard: RUN CLAIM ACQUIRED" not in second.stdout, (
        "a refused create must never print the claim sentinel"
    )
    _run_in_sh(f"rm -rf '{marker}' '{receipt}'\n", cwd=tmp_path)


def test_run_pr_guard_ledger_holds_denies_releases_and_spends() -> None:
    """The ledger's state machine, exercised directly: acquire / concurrent
    denial / owner-scoped release / sentinel-driven spend, with other runs
    unaffected."""
    scope = f"pytest-{uuid.uuid4().hex}"
    # The first flagged node takes the run's slot...
    assert acquire_run_pr_guard(scope, "node-1") == "acquired"
    # ...a CONCURRENT second node is denied while it is held...
    assert acquire_run_pr_guard(scope, "node-2") == "held"
    # ...the holder re-claiming (a node retry) stays allowed...
    assert acquire_run_pr_guard(scope, "node-1") == "acquired"
    # ...a DENIED node's settle never releases someone else's hold...
    assert settle_run_pr_guard(scope, "node-2", None) == "noop"
    assert acquire_run_pr_guard(scope, "node-2") == "held"
    # ...no claim observed -> the holder's own settle releases the slot...
    assert settle_run_pr_guard(scope, "node-1", "created the PR, no sentinel here") == "released"
    assert acquire_run_pr_guard(scope, "node-2") == "acquired"
    # ...and an OBSERVED claim spends the slot for the whole run.
    observed = f"noise {_GH_PR_GUARD_CLAIM_SENTINEL} more noise"
    assert settle_run_pr_guard(scope, "node-2", observed) == "spent"
    assert acquire_run_pr_guard(scope, "node-1") == "spent"
    # A different run is a different slot.
    assert acquire_run_pr_guard(f"pytest-{uuid.uuid4().hex}", "node-9") == "acquired"
    # An absent scope keeps the pre-FAR-1315 behaviour: unscoped, never shared.
    assert acquire_run_pr_guard(None, "node-9") == "unscoped"


@pytest.mark.asyncio
async def test_second_flagged_node_in_one_run_installs_a_pre_planted_refusal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The acceptance criterion, through the real install path: two flagged
    nodes in ONE run — the first gets a live guard, and once its claim is
    observed the second install is DENIED and pre-plants the same run-scoped
    marker, so its first ``gh pr create`` is refused instead of opening a
    second PR. The denial is logged loudly (claim status + scope + owner)."""
    scope = str(uuid.uuid4())
    marker = gh_pr_guard_marker_path(scope)

    node1 = _FakeSandbox()
    await apply_sandbox_policy(
        node1,
        read_only=False,
        git_credentials=None,
        egress_policy=None,
        egress_allowlist=None,
        single_pr_per_run=True,
        run_scope=scope,
        guard_owner="node-1",
    )
    assert len(node1.commands.runs) == 1
    first_script = node1.commands.runs[0]
    # The first flagged node gets a LIVE guard: nothing is pre-planted.
    assert 'mkdir -p "$MARKER"' not in first_script
    assert marker in first_script

    # The platform observes node-1's claim (sentinel fallback path; the
    # harvested receipt / delivered pr_url arms are covered separately below).
    assert settle_run_pr_guard(scope, "node-1", f"created {_GH_PR_GUARD_CLAIM_SENTINEL}") == "spent"

    with caplog.at_level(logging.WARNING, logger="modulo.core.pipeline_engine.sandbox_policy"):
        node2 = _FakeSandbox()
        await apply_sandbox_policy(
            node2,
            read_only=False,
            git_credentials=None,
            egress_policy=None,
            egress_allowlist=None,
            single_pr_per_run=True,
            run_scope=scope,
            guard_owner="node-2",
        )
    assert len(node2.commands.runs) == 1
    second_script = node2.commands.runs[0]
    # Same run-scoped marker path, but PRE-PLANTED: the marker directory
    # already exists, so node-2's first create hits the refusal branch.
    assert marker in second_script
    assert 'mkdir -p "$MARKER"' in second_script
    # The denial is observable in the policy log.
    assert "gh_guard_run_claim_denied" in caplog.text
    assert "spent" in caplog.text


def test_pre_planted_claim_refuses_the_first_create_in_a_second_sandbox(tmp_path: Path) -> None:
    """The pre-plant MECHANISM, executed: a shim installed with
    ``pre_spent=True`` refuses the FIRST ``gh pr create`` in its (separate)
    sandbox without ever calling the real gh — this is what node 2 of a
    flagged run executes. A refused create must leave NO claim receipt (the
    pre-plant is a bare ``mkdir``; only a successful create writes the
    receipt), so a denied node's harvest can never spend the run's claim."""
    bindir = tmp_path / "bin"
    _fake_gh(bindir)
    scope = f"pytest-{uuid.uuid4().hex}"
    marker = gh_pr_guard_marker_path(scope)
    receipt = gh_pr_claim_receipt_path(scope)
    _run_in_sh(f"rm -rf '{marker}'\n", cwd=tmp_path)
    _install_guard(bindir, tmp_path, marker, pre_spent=True)

    first = _gh(bindir, tmp_path, "gh pr create --title t")
    assert first.returncode != 0, "the pre-planted claim must refuse the FIRST create in this sandbox"
    assert "one-PR-per-run guard" in first.stderr
    assert not _gh_calls(bindir), "the real gh must never run when the run's claim is already spent"
    claim = _run_in_sh(f"test -f '{receipt}' && echo receipt-present\n", cwd=tmp_path)
    assert claim.returncode != 0, "a refused create must never leave a claim receipt"
    _run_in_sh(f"rm -rf '{marker}'\n", cwd=tmp_path)


@pytest.mark.asyncio
async def test_install_gh_pr_guard_via_exec_installs_then_denies() -> None:
    """The non-E2B install helper (Bundled Runner tier): a live install the
    first time, a pre-planted refusal for a second node of the SAME run."""
    scope = str(uuid.uuid4())
    marker = gh_pr_guard_marker_path(scope)
    scripts: list[str] = []

    async def _exec_ok(command: list[str]) -> SimpleNamespace:
        scripts.append(command[-1])
        return SimpleNamespace(exit_code=0, stderr="")

    first = await install_gh_pr_guard_via_exec(_exec_ok, run_scope=scope, guard_owner="node-1")
    assert first.status == "installed"
    assert first.marker_path == marker
    assert _GH_PR_GUARD_FINGERPRINT in scripts[0]
    assert 'mkdir -p "$MARKER"' not in scripts[0]

    second = await install_gh_pr_guard_via_exec(_exec_ok, run_scope=scope, guard_owner="node-2")
    assert second.status == "pre_planted"
    assert 'mkdir -p "$MARKER"' in scripts[1]


@pytest.mark.asyncio
async def test_install_gh_pr_guard_via_exec_reports_absent_and_failed() -> None:
    """Absence and failure are returned as loud statuses (never silent): no
    gh on PATH -> ``absent``; a non-zero install, a failed pre-plant, or a
    transport error -> ``failed`` with the diagnostic attached."""

    async def _exec_no_gh(command: list[str]) -> SimpleNamespace:
        return SimpleNamespace(exit_code=0, stderr="modulo: gh guard: WARNING no gh on PATH; nothing to guard")

    absent = await install_gh_pr_guard_via_exec(_exec_no_gh, run_scope=str(uuid.uuid4()), guard_owner="node-1")
    assert absent.status == "absent"
    assert "no gh on PATH" in absent.detail

    async def _exec_nonzero(command: list[str]) -> SimpleNamespace:
        return SimpleNamespace(
            exit_code=1,
            stderr="modulo: gh guard: FAILED - a gh exists on PATH but none could be guarded",
        )

    failed = await install_gh_pr_guard_via_exec(_exec_nonzero, run_scope=str(uuid.uuid4()), guard_owner="node-1")
    assert failed.status == "failed"

    async def _exec_boom(command: list[str]) -> SimpleNamespace:
        raise RuntimeError("exec transport down")

    boom = await install_gh_pr_guard_via_exec(_exec_boom, run_scope=str(uuid.uuid4()), guard_owner="node-1")
    assert boom.status == "failed"
    assert "exec transport down" in boom.detail


@pytest.mark.asyncio
async def test_install_gh_pr_guard_via_exec_failed_pre_plant_is_reported_as_absent() -> None:
    """A denied claim whose marker could NOT be planted must not be reported
    as a working guard: without the planted marker the first create in the
    (already-spent) run would go through — the run-scope enforcement is gone."""
    scope = str(uuid.uuid4())
    # Hold the slot with another node so the install is denied (pre-spent).
    assert acquire_run_pr_guard(scope, "node-1") == "acquired"

    async def _exec_plant_failed(command: list[str]) -> SimpleNamespace:
        return SimpleNamespace(
            exit_code=0,
            stderr="modulo: gh guard: WARNING could not pre-plant the run claim at /tmp/marker",
        )

    result = await install_gh_pr_guard_via_exec(_exec_plant_failed, run_scope=scope, guard_owner="node-2")
    assert result.status == "failed"
    assert "could not pre-plant" in result.detail


# --- FAR-1315 gate hardening: spend evidence that survives truncation AND
# --- cannot be forged by merely READING the shim ----------------------------


def shim_created_pr_stdout(tmp_path: Path) -> str:
    """STDOUT produced by a REAL successful ``gh pr create`` through the shim.

    Installed and executed under a real shell (Git Bash on Windows), so the
    bytes are genuinely shim-produced. The dispatch-level observation-channel
    tests (E2B + runner_docker) feed THIS into a real node dispatch instead of
    hand-writing the sentinel literal, proving the shim's output -> dispatch
    capture -> real ``settle_run_pr_guard`` channel end to end.
    """
    bindir = tmp_path / "bin"
    _fake_gh(bindir)
    scope = f"pytest-{uuid.uuid4().hex}"
    marker = gh_pr_guard_marker_path(scope)
    _run_in_sh(f"rm -rf '{marker}'\n", cwd=tmp_path)
    _install_guard(bindir, tmp_path, marker)
    result = _gh(bindir, tmp_path, "gh pr create --title t")
    assert result.returncode == 0, f"the setup create must succeed: {result.stdout}\n{result.stderr}"
    _run_in_sh(f"rm -rf '{marker}'\n", cwd=tmp_path)
    return result.stdout


def test_settle_spends_from_a_harvested_receipt_even_when_the_sentinel_was_truncated() -> None:
    """MAJOR 1 (truncation): a node that created the PR and then emitted more
    than the 512 KB drain window LOSES the mid-stream sentinel from the
    captured output. The platform-side receipt harvest (claim_receipt=True)
    must still spend the run - releasing it here would let the next flagged
    node install a live guard and open a SECOND PR."""
    scope = f"pytest-{uuid.uuid4().hex}"
    assert acquire_run_pr_guard(scope, "node-1") == "acquired"
    # Streams carry ONLY post-create noise: the sentinel was drained away.
    assert settle_run_pr_guard(scope, "node-1", "x" * 100, claim_receipt=True) == "spent"
    assert acquire_run_pr_guard(scope, "node-2") == "spent"


def test_settle_ignores_a_sentinel_when_the_harvest_confirms_no_receipt() -> None:
    """MAJOR 2 (shim-read fail-closed DoS): the sentinel is a fixed literal
    embedded in a mode-755 shim, so ``cat $(command -v gh)`` (or prompt
    injection that echoes it) puts it in captured output. When the harvest RAN
    against a LIVE shim and confirmed no receipt, the sentinel must be IGNORED
    and the holder's hold RELEASED - never spent, which would pre-plant every
    later flagged node and deliver zero PRs.

    FAR-1315 re-gate: the definitive negative now ALSO requires the install
    status (``installed``/``pre_planted``) - a receipt probe against a path no
    shim ever wrote is meaningless (see the absent-install companion below).
    """
    scope = f"pytest-{uuid.uuid4().hex}"
    assert acquire_run_pr_guard(scope, "node-1") == "acquired"
    shim_read = f"noise {_GH_PR_GUARD_CLAIM_SENTINEL} more noise"  # what reading the shim yields
    assert (
        settle_run_pr_guard(scope, "node-1", shim_read, claim_receipt=False, guard_install_status="installed")
        == "released"
    )
    # NOT spent: a later flagged node still gets its chance at the one PR.
    assert acquire_run_pr_guard(scope, "node-2") == "acquired"


def test_settle_receipt_false_with_an_absent_install_is_meaningless() -> None:
    """FAR-1315 re-gate MAJOR 2(b) - FALSE RELEASE: on the shipped runner
    image there is no ``gh``, so no shim is installed and the receipt probe
    runs against a path NO SHIM EVER WROTE - it answers ABSENT exactly like a
    real "no receipt" probe. Read as definitive, it would suppress the sentinel
    arm and release a genuinely unguarded create. With the install status
    threaded, ``absent``/``failed``/unthreaded all leave the receipt UNKNOWN,
    so the sentinel fallback still spends the run.
    """
    shim_sentinel = f"noise {_GH_PR_GUARD_CLAIM_SENTINEL}"
    for status in ("absent", "failed", None):
        scope = f"pytest-{uuid.uuid4().hex}"
        assert acquire_run_pr_guard(scope, "node-1") == "acquired"
        assert (
            settle_run_pr_guard(
                scope,
                "node-1",
                shim_sentinel,
                claim_receipt=False,
                guard_install_status=status,  # type: ignore[arg-type]
            )
            == "spent"
        ), f"install status {status!r} must not make a False receipt definitive"
        assert acquire_run_pr_guard(scope, "node-2") == "spent"


def test_settle_receipt_false_outranks_a_url_valid_pr_url() -> None:
    """FAR-1315 re-gate MAJOR 2(a) - FALSE SPEND: ``pr_url`` comes from the
    node's own ``output.json`` and is validated for URL SYNTAX only, so a node
    whose ``gh pr create`` FAILED can still report a URL-shaped ``pr_url``.
    When the LIVE shim's receipt harvest confirms no create happened, that
    agent-authored text must NOT outrank the definitive negative: the run is
    released, never spent (spending would pre-plant every later flagged node
    and the run would deliver nothing).
    """
    url = "https://github.com/org/repo/pull/42"
    scope = f"pytest-{uuid.uuid4().hex}"
    assert acquire_run_pr_guard(scope, "node-1") == "acquired"
    # The URL is even present in the captured stream - still not spent: the
    # definitive receipt=False from a LIVE install outranks every other signal.
    assert (
        settle_run_pr_guard(
            scope,
            "node-1",
            f"created {url}",
            pr_url=url,
            claim_receipt=False,
            guard_install_status="installed",
        )
        == "released"
    )
    assert acquire_run_pr_guard(scope, "node-2") == "acquired"


def test_settle_uncorroborated_pr_url_never_spends() -> None:
    """FAR-1315 re-gate MAJOR 2(a): a URL-valid ``pr_url`` in ``output.json``
    ALONE is agent-authored text - it spends only when the platform's own
    capture of the transcript corroborates it (the URL also appears in the
    streams, i.e. the extraction the FAR-188 marker persists). Without that
    corroboration and with no receipt/sentinel evidence the hold is released.
    """
    url = "https://github.com/org/repo/pull/7"
    scope = f"pytest-{uuid.uuid4().hex}"
    assert acquire_run_pr_guard(scope, "node-1") == "acquired"
    assert settle_run_pr_guard(scope, "node-1", "no PR was created here", pr_url=url, claim_receipt=None) == "released"
    assert acquire_run_pr_guard(scope, "node-2") == "acquired"

    # Corroborated (same URL seen in the captured stream) -> spends.
    scope2 = f"pytest-{uuid.uuid4().hex}"
    assert acquire_run_pr_guard(scope2, "node-1") == "acquired"
    assert settle_run_pr_guard(scope2, "node-1", f"opened {url}", pr_url=url, claim_receipt=None) == "spent"
    assert acquire_run_pr_guard(scope2, "node-2") == "spent"


def test_settle_falls_back_to_the_sentinel_only_when_the_harvest_is_unavailable() -> None:
    """The bounded residual channel: with claim_receipt=None (sandbox already
    destroyed, exec failed) the sentinel remains the fallback spend signal -
    this is what keeps a genuine create spent when only the stream survived."""
    scope = f"pytest-{uuid.uuid4().hex}"
    assert acquire_run_pr_guard(scope, "node-1") == "acquired"
    assert settle_run_pr_guard(scope, "node-1", "no claim evidence here") == "released"
    assert acquire_run_pr_guard(scope, "node-2") == "acquired"
    assert settle_run_pr_guard(scope, "node-2", f"noise {_GH_PR_GUARD_CLAIM_SENTINEL}", claim_receipt=None) == "spent"
    assert acquire_run_pr_guard(scope, "node-3") == "spent"


def test_settle_spends_on_a_corroborated_delivered_pr_url_and_never_on_junk() -> None:
    """The pr_url arm: a platform-parsed delivery, CORROBORATED by the
    platform's own transcript capture, spends the run even when sentinel AND
    receipt are both gone (robust to output.json parse noise), and also when
    the guard was ABSENT but a PR was still delivered. Junk under the key
    (``\"N/A\"``, a non-http string) must never spend - a sloppy agent writing
    pr_url: \"N/A\" would otherwise burn the run's attempt."""
    url = "https://github.com/org/repo/pull/42"
    scope = f"pytest-{uuid.uuid4().hex}"
    assert acquire_run_pr_guard(scope, "node-1") == "acquired"
    assert settle_run_pr_guard(scope, "node-1", f"gh: {url}", pr_url=url) == "spent"
    assert acquire_run_pr_guard(scope, "node-2") == "spent"

    scope2 = f"pytest-{uuid.uuid4().hex}"
    assert acquire_run_pr_guard(scope2, "node-1") == "acquired"
    assert settle_run_pr_guard(scope2, "node-1", None, pr_url="N/A", claim_receipt=False) == "released"
    assert acquire_run_pr_guard(scope2, "node-2") == "acquired"


def test_corroboration_pr_url_regex_is_pinned_to_node_runners_extraction() -> None:
    """The corroboration arm's two regexes are ONE pattern kept in two places.

    ``sandbox_policy`` cannot import ``node_runner`` (it must stay
    dependency-free), so the PR-URL shape it matches against the
    platform-captured streams (``_GH_PR_URL_RE``) is a MIRROR of the pattern
    node_runner extracts the FAR-188 marker's ``pr_url`` with
    (``_PR_URL_PATTERN``). Settle's ``pr_url`` arm compares the reported URL
    against the streams with this mirror, so an isolated edit to either copy
    would silently change spend/release outcomes with no other failing test.
    Pin both copies together (pattern AND flags) so drift fails here."""
    import modulo.core.pipeline_engine.node_runner as node_runner
    import modulo.core.pipeline_engine.sandbox_policy as sandbox_policy

    assert sandbox_policy._GH_PR_URL_RE.pattern == node_runner._PR_URL_PATTERN.pattern, (
        "sandbox_policy's corroboration mirror must stay identical to node_runner's extraction"
    )
    assert sandbox_policy._GH_PR_URL_RE.flags == node_runner._PR_URL_PATTERN.flags, (
        "a flag change (e.g. IGNORECASE) on one copy only would also desynchronise matching"
    )


def test_gh_pr_guard_marker_and_receipt_paths_agree_for_non_canonical_run_ids() -> None:
    """FAR-1315 latent fix: the marker/receipt PATH used to canonicalise a
    UUID run scope on the install side (``str(spec.run_id)``) but sanitise the
    RAW run id on the harvest/settle side. A braced/uppercase/urn run id would
    make the probe read a path the shim never wrote and report a definitive
    ``False`` against it (a false release). Both sides now canonicalise
    identically: every form of the same UUID yields ONE marker path, and the
    receipt path hangs off it."""
    canonical = "11111111-2222-3333-4444-555555555555"
    forms = [
        canonical,
        canonical.upper(),
        f"{{{canonical}}}",
        f"urn:uuid:{canonical}",
        canonical.replace("-", ""),
    ]
    marker_paths = {gh_pr_guard_marker_path(form) for form in forms}
    assert len(marker_paths) == 1, f"every run-id form must map to one marker path, got {marker_paths}"
    receipt_paths = {gh_pr_claim_receipt_path(form) for form in forms}
    assert receipt_paths == {f"{next(iter(marker_paths))}/{_GH_PR_GUARD_CLAIM_RECEIPT}"}

    # The LEDGER keys identically too, so a non-canonical run id still shares
    # one claim slot across nodes.
    scope = forms[2]
    reset_run_pr_guard_claims()
    try:
        assert acquire_run_pr_guard(scope, "node-1") == "acquired"
        assert acquire_run_pr_guard(canonical.upper(), "node-2") == "held"
    finally:
        reset_run_pr_guard_claims()


def test_settle_records_spent_even_without_a_prior_ledger_entry() -> None:
    """Spend evidence must not be dropped just because the ledger entry is
    gone (evicted by the bound, or the claim lived in a dead process): the
    settle records SPENT so a later flagged node is still denied."""
    scope = f"pytest-{uuid.uuid4().hex}"
    # No prior acquire - the entry does not exist.
    assert settle_run_pr_guard(scope, "node-1", None, claim_receipt=True) == "spent"
    assert acquire_run_pr_guard(scope, "node-2") == "spent"
    # Non-spending evidence with no entry stays a noop (never creates holds).
    other = f"pytest-{uuid.uuid4().hex}"
    assert settle_run_pr_guard(other, "node-1", "nothing to see") == "noop"
    assert acquire_run_pr_guard(other, "node-2") == "acquired"


def test_shim_produced_stdout_spends_the_run_claim_through_the_real_settle(tmp_path: Path) -> None:
    """The observation channel at the unit level: stdout produced by the REAL
    shim's successful create spends the run through the REAL settle - with the
    harvest unavailable (claim_receipt=None), exactly the fallback arm a
    dispatch reaches when its sandbox is already gone."""
    stdout = shim_created_pr_stdout(tmp_path)
    assert "modulo: one-PR-per-run guard: RUN CLAIM ACQUIRED" in stdout
    scope = f"pytest-{uuid.uuid4().hex}"
    assert acquire_run_pr_guard(scope, "node-1") == "acquired"
    assert settle_run_pr_guard(scope, "node-1", stdout, claim_receipt=None) == "spent"
    assert acquire_run_pr_guard(scope, "node-2") == "spent"


@pytest.mark.asyncio
async def test_harvest_gh_pr_claim_via_exec_parses_the_probe_reply() -> None:
    """The harvest's contract: present / absent / unusable. A NON-ZERO probe
    exit must be ``None`` (unavailable) even if its stdout happens to carry
    the present token - a failed probe can never be read as a claim."""
    scope = str(uuid.uuid4())

    async def _present(command: list[str]) -> SimpleNamespace:
        assert command[:2] == ["sh", "-c"]
        return SimpleNamespace(exit_code=0, stdout="MODULO_CLAIM_RECEIPT_PRESENT", stderr="")

    async def _absent(command: list[str]) -> SimpleNamespace:
        return SimpleNamespace(exit_code=0, stdout="MODULO_CLAIM_RECEIPT_ABSENT", stderr="")

    async def _silent(command: list[str]) -> SimpleNamespace:
        return SimpleNamespace(exit_code=0, stdout="", stderr="")

    async def _boom(command: list[str]) -> SimpleNamespace:
        raise RuntimeError("exec transport down")

    async def _nonzero(command: list[str]) -> SimpleNamespace:
        return SimpleNamespace(exit_code=1, stdout="MODULO_CLAIM_RECEIPT_PRESENT", stderr="sh: boom")

    assert await harvest_gh_pr_claim_via_exec(_present, run_scope=scope) is True
    assert await harvest_gh_pr_claim_via_exec(_absent, run_scope=scope) is False
    assert await harvest_gh_pr_claim_via_exec(_silent, run_scope=scope) is None
    assert await harvest_gh_pr_claim_via_exec(_boom, run_scope=scope) is None
    assert await harvest_gh_pr_claim_via_exec(_nonzero, run_scope=scope) is None
    # The probe targets THIS run's receipt path (sanitised scope, quoted).
    captured: list[str] = []

    async def _capture(command: list[str]) -> SimpleNamespace:
        captured.append(command[-1])
        return SimpleNamespace(exit_code=0, stdout="MODULO_CLAIM_RECEIPT_ABSENT", stderr="")

    await harvest_gh_pr_claim_via_exec(_capture, run_scope=scope)
    assert gh_pr_claim_receipt_path(scope) in captured[0]


@pytest.mark.asyncio
async def test_harvest_reads_a_real_receipt_written_by_a_real_create(tmp_path: Path) -> None:
    """REAL shell end to end: a successful create through the installed shim
    leaves the receipt, the REAL probe script reads it (``True``); removing it
    flips the harvest to ``False``. Not a mocked exec_command - the probe's
    own bytes are parsed by the real helper."""
    bindir = tmp_path / "bin"
    _fake_gh(bindir)
    scope = f"pytest-{uuid.uuid4().hex}"
    marker = gh_pr_guard_marker_path(scope)
    _run_in_sh(f"rm -rf '{marker}'\n", cwd=tmp_path)
    _install_guard(bindir, tmp_path, marker)
    first = _gh(bindir, tmp_path, "gh pr create --title t")
    assert first.returncode == 0

    async def _real_exec(command: list[str]) -> SimpleNamespace:
        # command == ["sh", "-c", <probe>]; execute the probe for real.
        res = _run_in_sh(command[2], cwd=tmp_path)
        return SimpleNamespace(exit_code=res.returncode, stdout=res.stdout, stderr=res.stderr)

    assert await harvest_gh_pr_claim_via_exec(_real_exec, run_scope=scope) is True
    _run_in_sh(f"rm -f '{gh_pr_claim_receipt_path(scope)}'\n", cwd=tmp_path)
    assert await harvest_gh_pr_claim_via_exec(_real_exec, run_scope=scope) is False
    _run_in_sh(f"rm -rf '{marker}'\n", cwd=tmp_path)


def _ghless_path_bin(tmp_path: Path) -> Path:
    """A single PATH directory holding the install script's needed tools but
    NO ``gh`` - the shipped runner image's shape (no gh anywhere on PATH).
    The script only needs ``tr`` when no ``gh`` exists (the per-directory
    grep/chmod/cp/mv arms are unreachable without one)."""
    toolbin = tmp_path / "shipped-image-path"
    toolbin.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        candidates = [
            Path(r"C:\Program Files\Git\usr\bin\tr.exe"),
            Path(r"C:\Program Files (x86)\Git\usr\bin\tr.exe"),
        ]
        src = next((candidate for candidate in candidates if candidate.is_file()), None)
        if src is None:
            pytest.skip("Git Bash coreutils not available on this Windows system")
        shutil.copy2(src, toolbin / "tr.exe")
    else:
        found = shutil.which("tr")
        if not found:
            pytest.skip("tr not available on this system")
        shutil.copy2(found, toolbin / "tr")
    return toolbin


@pytest.mark.asyncio
async def test_install_via_exec_runs_the_real_script_and_reports_absent_on_a_gh_less_path(
    tmp_path: Path,
) -> None:
    """MAJOR 3 (tier claim, non-mock): the REAL install script executed under
    a real shell against a gh-less PATH - the shipped first-party runner image
    (no ``gh``, read-only rootfs, no writable PATH dir) resolves to exactly
    this - reports ``absent`` with the script's own diagnostic. The previous
    test only fed a hand-written stderr string through the classifier; this one
    executes the script the tier actually runs."""
    toolbin = _ghless_path_bin(tmp_path)
    posix_bin = _posixify(str(toolbin))

    async def _real_exec(command: list[str]) -> SimpleNamespace:
        body = f"PATH={posix_bin}\nexport PATH\n{command[2]}\n"
        res = _run_in_sh(body, cwd=tmp_path)
        return SimpleNamespace(exit_code=res.returncode, stdout=res.stdout, stderr=res.stderr)

    result = await install_gh_pr_guard_via_exec(_real_exec, run_scope=str(uuid.uuid4()), guard_owner="node-1")
    assert result.status == "absent", f"the shipped image shape must report absent: {result.detail}"
    assert "no gh on PATH" in result.detail
    assert "NOT platform-guarded" in result.detail


def test_run_pr_guard_ledger_is_bounded() -> None:
    """The process-local ledger must not grow without limit in a long-lived
    engine process: eviction kicks in past the cap, the NEWEST entries (the
    ones most likely to have flagged nodes in flight) survive, and the cap
    holds afterwards."""
    import modulo.core.pipeline_engine.sandbox_policy as sandbox_policy

    reset_run_pr_guard_claims()
    cap = sandbox_policy._MAX_RUN_PR_GUARD_CLAIMS
    last = f"pytest-bound-{cap + 4}"
    for i in range(cap + 5):
        assert acquire_run_pr_guard(f"pytest-bound-{i}", "node-1") == "acquired"
    assert len(sandbox_policy._RUN_PR_GUARD_CLAIMS) <= cap
    # The newest scope survived eviction: its slot is still HELD by node-1.
    assert acquire_run_pr_guard(last, "node-2") == "held"
    # An evicted (oldest) scope is forgotten - it can be claimed again.
    assert acquire_run_pr_guard("pytest-bound-0", "node-2") == "acquired"
    reset_run_pr_guard_claims()


# ---------------------------------------------------------------------------
# FAR-1315 re-gate MAJOR 1: the BOUNDED, cancellation-safe harvest wrapper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_harvest_bounded_returns_the_probe_result() -> None:
    """The wrapper is transparent for the normal outcomes: present/absent/
    unusable pass straight through to the caller's settle."""
    scope = str(uuid.uuid4())

    async def _present(command: list[str]) -> SimpleNamespace:
        return SimpleNamespace(exit_code=0, stdout="MODULO_CLAIM_RECEIPT_PRESENT", stderr="")

    async def _absent(command: list[str]) -> SimpleNamespace:
        return SimpleNamespace(exit_code=0, stdout="MODULO_CLAIM_RECEIPT_ABSENT", stderr="")

    async def _boom(command: list[str]) -> SimpleNamespace:
        raise RuntimeError("exec transport down")

    assert await harvest_gh_pr_claim_bounded(_present, run_scope=scope) is True
    assert await harvest_gh_pr_claim_bounded(_absent, run_scope=scope) is False
    assert await harvest_gh_pr_claim_bounded(_boom, run_scope=scope) is None


@pytest.mark.asyncio
async def test_harvest_bounded_timeout_cancels_and_drains_the_probe() -> None:
    """The re-gate's shield finding: on TIMEOUT the old ``wait_for(shield(...))``
    returned while the inner probe task kept running against a container the
    teardown was about to destroy (and never awaited it). The wrapper must
    return ``None`` (receipt unknown) AND leave no live probe behind."""
    started = asyncio.Event()
    probe_cancelled = asyncio.Event()

    async def _hang(command: list[str]) -> SimpleNamespace:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            probe_cancelled.set()
            raise
        return SimpleNamespace(exit_code=0, stdout="", stderr="")  # pragma: no cover

    result = await harvest_gh_pr_claim_bounded(_hang, run_scope=str(uuid.uuid4()), timeout=0.05)
    assert result is None, "a timed-out harvest must report the receipt as UNKNOWN"
    # The probe task must be cancelled, not left running against the container
    # the teardown that follows is about to destroy.
    await asyncio.wait_for(probe_cancelled.wait(), timeout=5)


@pytest.mark.asyncio
async def test_harvest_bounded_cancellation_is_reraised_after_the_probe_is_drained() -> None:
    """The re-gate's cancellation finding: ``CancelledError`` must PROPAGATE
    (so the dispatch can re-raise it after teardown) but only AFTER the probe
    task has been cancelled and drained - never while it still runs."""
    outer = asyncio.Event()
    drained = asyncio.Event()

    async def _hang(command: list[str]) -> SimpleNamespace:
        outer.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            drained.set()
            raise
        return SimpleNamespace(exit_code=0, stdout="", stderr="")  # pragma: no cover

    async def _driver() -> None:
        await harvest_gh_pr_claim_bounded(_hang, run_scope=str(uuid.uuid4()))

    task = asyncio.ensure_future(_driver())
    await asyncio.wait_for(outer.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # By the time the CancelledError reached the caller, the probe was already
    # cancelled and drained inside the wrapper.
    await asyncio.wait_for(drained.wait(), timeout=5)
    assert task.done()


# ---------------------------------------------------------------------------
# FAR-1315 coverage hardening: the guard helpers' edge / defensive branches.
#
# These drive the small private helpers directly (and through ``settle`` where
# a public surface exists) for the cases the end-to-end dispatch tests do not
# reach: an absent scope, ledger-bound eviction on the settle path, a
# non-parsing URL, empty streams, and every outcome of the bounded-harvest
# drain (done / nested-cancel / stubborn / cancelled / BaseException).
# ---------------------------------------------------------------------------


def test_canonical_scope_uuid_returns_none_for_an_absent_scope() -> None:
    """An empty / absent scope canonicalises to ``None``: no run id means no
    cross-node sharing, so the key builder short-circuits before parsing."""
    import modulo.core.pipeline_engine.sandbox_policy as sandbox_policy

    assert sandbox_policy._canonical_scope_uuid(None) is None
    assert sandbox_policy._canonical_scope_uuid("") is None


def test_settle_run_pr_guard_is_a_noop_without_a_usable_run_scope() -> None:
    """A settle with no usable scope is a noop — it runs in a dispatch
    ``finally`` and must never raise nor create a hold out of thin air."""
    assert settle_run_pr_guard(None, "node-1", "created the PR") == "noop"
    assert settle_run_pr_guard("", "node-1", "created the PR") == "noop"


def test_settle_spend_without_an_entry_still_honours_the_ledger_bound() -> None:
    """Spend evidence for a scope with no ledger entry records SPENT, and that
    insertion is still bounded — a long-lived engine process cannot grow the
    ledger without limit through settles alone."""
    import modulo.core.pipeline_engine.sandbox_policy as sandbox_policy

    reset_run_pr_guard_claims()
    cap = sandbox_policy._MAX_RUN_PR_GUARD_CLAIMS
    try:
        for i in range(cap):
            assert acquire_run_pr_guard(f"pytest-settle-bound-{i}", "node-1") == "acquired"
        # No prior entry for the new scope -> the settle inserts then evicts.
        assert settle_run_pr_guard("pytest-settle-bound-new", "node-1", None, claim_receipt=True) == "spent"
        assert len(sandbox_policy._RUN_PR_GUARD_CLAIMS) <= cap
    finally:
        reset_run_pr_guard_claims()


def test_is_valid_delivered_pr_url_rejects_a_url_that_fails_to_parse() -> None:
    """A ``pr_url`` that makes ``urlsplit`` raise is never a spend signal: the
    parse guard fail-closes to ``False`` instead of propagating."""
    import modulo.core.pipeline_engine.sandbox_policy as sandbox_policy

    assert sandbox_policy._is_valid_delivered_pr_url("http://[::1") is False
    assert sandbox_policy._is_valid_delivered_pr_url("N/A") is False
    assert sandbox_policy._is_valid_delivered_pr_url("https://github.com/org/repo/pull/1") is True


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/org/repo/pull/1",  # valid http(s) with a netloc
        "http://x",
        "https://",  # empty netloc
        "ftp://x",  # unsupported scheme
        "",  # blank
        "   ",  # whitespace-only
        "N/A",  # scheme-less token
        "http://[::1",  # malformed authority -> urlsplit raises
    ],
)
def test_is_valid_delivered_pr_url_is_pinned_to_the_classifier_spec(url: str) -> None:
    """The delivered-``pr_url`` validity mirror agrees with the classifier spec.

    ``sandbox_policy`` cannot import ``classify`` (it must stay dependency-free),
    so ``_is_valid_delivered_pr_url`` MIRRORS ``classify._is_valid_pr_url``.
    Settle's ``pr_url`` arm gates spend on the mirror, so an isolated edit to
    either copy would silently change spend/release outcomes with no other
    failing test. Pin both truth tables together so drift fails here — the same
    protection ``test_corroboration_pr_url_regex_is_pinned_to_node_runners_extraction``
    gives the ``_GH_PR_URL_RE`` mirror."""
    import modulo.core.pipeline_engine.classify as classify
    import modulo.core.pipeline_engine.sandbox_policy as sandbox_policy

    assert sandbox_policy._is_valid_delivered_pr_url(url) is classify._is_valid_pr_url(url), (
        f"the delivered-pr_url mirror must agree with the classifier spec on {url!r}"
    )


def test_pr_url_corroboration_skips_empty_streams_and_non_matching_urls() -> None:
    """The stream scan skips falsy streams and keeps scanning past a
    non-matching PR URL until the reported one is found (and rejects a missing
    or blank ``pr_url`` outright)."""
    import modulo.core.pipeline_engine.sandbox_policy as sandbox_policy

    target = "https://github.com/org/repo/pull/1"
    assert (
        sandbox_policy._pr_url_seen_in_streams(
            target,
            (None, "", f"first saw https://github.com/org/repo/pull/7 then {target}"),
        )
        is True
    )
    assert sandbox_policy._pr_url_seen_in_streams(None, (target,)) is False
    assert sandbox_policy._pr_url_seen_in_streams("   ", (target,)) is False


async def test_cancel_and_drain_harvest_skips_an_already_done_probe() -> None:
    """A probe that finished before the drain was asked to cancel it is left
    alone — there is nothing to cancel and the wait returns immediately."""
    import modulo.core.pipeline_engine.sandbox_policy as sandbox_policy

    probe: asyncio.Future[bool | None] = asyncio.ensure_future(asyncio.sleep(0, result=True))
    await probe
    await sandbox_policy._cancel_and_drain_harvest(probe)
    assert probe.done()


async def test_cancel_and_drain_harvest_swallows_a_nested_cancellation() -> None:
    """A cancellation landing while the drain itself waits is swallowed: the
    probe is already cancelled, and the caller re-raises its OWN recorded
    cancellation after teardown."""
    import modulo.core.pipeline_engine.sandbox_policy as sandbox_policy

    released = asyncio.Event()

    async def _probe() -> bool | None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            released.set()
            raise
        return None  # pragma: no cover - the sleep never returns normally

    probe: asyncio.Future[bool | None] = asyncio.ensure_future(_probe())
    driver = asyncio.ensure_future(sandbox_policy._cancel_and_drain_harvest(probe))
    await asyncio.sleep(0)  # let the drain start waiting on the probe
    driver.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await driver
    await asyncio.wait_for(released.wait(), timeout=5)
    assert probe.done()


async def test_cancel_and_drain_harvest_consumes_a_probe_that_outlives_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe that ignores its cancellation and outlives the drain bound gets
    a done-callback that consumes its later outcome — a late failure is never
    surfaced as asyncio's 'exception was never retrieved'."""
    import modulo.core.pipeline_engine.sandbox_policy as sandbox_policy

    monkeypatch.setattr(sandbox_policy, "_HARVEST_DRAIN_TIMEOUT", 0.05)
    finished = asyncio.Event()

    async def _stubborn() -> bool | None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            # Acknowledge the cancellation but keep running: this models a
            # probe that ignores its cancel long enough to outlive the bound.
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
        await asyncio.sleep(0.05)
        finished.set()
        raise RuntimeError("probe failed after the drain bound")

    probe: asyncio.Future[bool | None] = asyncio.ensure_future(_stubborn())
    await asyncio.sleep(0)  # let the probe reach its first await before we cancel it
    await sandbox_policy._cancel_and_drain_harvest(probe)
    assert not probe.done(), "the stubborn probe must still be running when the bound expires"
    await asyncio.wait_for(finished.wait(), timeout=5)
    await asyncio.sleep(0)  # let the done-callback run
    assert probe.done()
    # The callback retrieved it, so this call does not raise/consume anything new.
    assert isinstance(probe.exception(), RuntimeError)


async def test_consume_harvest_probe_outcome_handles_every_terminal_state() -> None:
    """The done-callback is total: a cancelled probe is a no-op, a clean probe
    is a no-op, and a failed probe's exception is retrieved (never leaked)."""
    import modulo.core.pipeline_engine.sandbox_policy as sandbox_policy

    cancelled: asyncio.Future[bool | None] = asyncio.ensure_future(asyncio.sleep(3600))
    cancelled.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await cancelled
    sandbox_policy._consume_harvest_probe_outcome(cancelled)

    clean: asyncio.Future[bool | None] = asyncio.ensure_future(asyncio.sleep(0, result=True))
    await clean
    sandbox_policy._consume_harvest_probe_outcome(clean)
    assert clean.exception() is None

    async def _boom() -> bool | None:
        raise RuntimeError("late probe failure")

    failed: asyncio.Future[bool | None] = asyncio.ensure_future(_boom())
    with contextlib.suppress(RuntimeError):
        await failed
    sandbox_policy._consume_harvest_probe_outcome(failed)
    assert isinstance(failed.exception(), RuntimeError)


async def test_harvest_bounded_reports_unknown_when_the_probe_is_cancelled() -> None:
    """A probe coroutine that raises ``CancelledError`` itself leaves its task
    cancelled; the wrapper reports the receipt as unknown (``None``) instead of
    propagating the cancellation into the caller's teardown forensics."""
    import modulo.core.pipeline_engine.sandbox_policy as sandbox_policy

    async def _cancelled(command: list[str]) -> SimpleNamespace:
        raise asyncio.CancelledError

    assert await sandbox_policy.harvest_gh_pr_claim_bounded(_cancelled, run_scope=str(uuid.uuid4())) is None


async def test_harvest_bounded_reports_unknown_on_a_base_exception() -> None:
    """Even a non-``Exception`` BaseException from the probe becomes an unknown
    receipt (``None``): the finally caller must never see an unexpected failure
    escape the best-effort harvest."""
    import modulo.core.pipeline_engine.sandbox_policy as sandbox_policy

    class _ProbeExploded(BaseException):
        pass

    async def _explode(command: list[str]) -> SimpleNamespace:
        raise _ProbeExploded("probe blew up")

    assert await sandbox_policy.harvest_gh_pr_claim_bounded(_explode, run_scope=str(uuid.uuid4())) is None
