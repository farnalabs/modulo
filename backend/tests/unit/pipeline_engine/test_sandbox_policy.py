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
        if not os.path.isfile(git_bash):
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
        assert os.path.isfile("/tmp/modulo-git-refuse-helper.sh")
    else:
        win_temp = os.environ.get("TEMP", os.environ.get("TMP", ""))
        if win_temp:
            assert os.path.isfile(os.path.join(win_temp, "modulo-git-refuse-helper.sh"))


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
        if os.path.isfile(git_bash):
            sh_cmd = [git_bash, "-c", helper]
    allowed = subprocess.run(  # noqa: S603 - executing our own helper script in tests
        sh_cmd,
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
        env={"GITHUB_TOKEN": token, "PATH": os.environ["PATH"]},
        timeout=60,
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
