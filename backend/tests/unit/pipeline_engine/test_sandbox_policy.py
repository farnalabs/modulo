"""Unit tests for the FAR-212 PR B sandbox policy enforcement surface.

Covers the script builders (read-only chmod, git-credential scoped/none,
selected-mode egress allowlist), the ``apply_sandbox_policy`` step ordering,
the PipelineGraphNode field validation (read_only / git_credentials), and the
updated capability derivation (write_files / git_credentials now mechanically
derivable from validated + enforced config).
"""

from __future__ import annotations

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
