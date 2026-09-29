"""Unit tests for the FAR-212 PR B sandbox policy enforcement surface.

Covers the script builders (read-only chmod, git-credential scoped/none,
selected-mode egress allowlist), the ``apply_sandbox_policy`` step ordering,
the PipelineGraphNode field validation (read_only / git_credentials), and the
updated capability derivation (write_files / git_credentials now mechanically
derivable from validated + enforced config).
"""

from __future__ import annotations

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
    _GH_PR_GUARD_FINGERPRINT,
    apply_sandbox_policy,
    build_egress_selected_script,
    build_gh_pr_guard_script,
    build_git_none_script,
    build_git_scoped_script,
    build_read_only_script,
    gh_pr_guard_marker_path,
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


def _install_guard(bindir: Path, workdir: Path, marker: str) -> None:
    posix_bin = _posixify(str(bindir))
    install = workdir / "install.sh"
    install.write_text(build_gh_pr_guard_script(marker), encoding="utf-8", newline="\n")
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
