"""FAR-803 P1 verification suite for the Managed Workspace Inputs spine.

Cross-cutting verification of the merged MWI modules (FAR-796/797/798/800/802):

1. Status-transition invariants: resolution failure, checkout failure,
   partial multi-input failure. The self-consistent-state contract at the
   orchestration layer is that a resolved input list is only ever returned
   COMPLETE (never a partial list), provisioning is ordered per input
   (setup, clone, teardown), a checkout failure short-circuits before later
   inputs, and the resolved ``resolved_sha`` is exactly what the clone
   checks out (``resolved`` implies the checkout SHA, the "provisioned
   implies checked out" invariant).
2. Multi-host token isolation: host A gets token A only; an input without
   a connector gets NO credential material at all.
3. Adversarial dest containment: traversal, empty, ``.``, ``/home/user``,
   ``.git`` component, denylist components, depth abuse.
4. Workspace-inputs opt-in semantics (absent config is valid) and the
   ``multi_host`` BOOLEAN capability derivation (FAR-798).
5. Deterministic stdout SHA extraction fixture (realistic multi-line
   output incl. log noise).
6. Drift detection incl. the drift-check-fails-but-the-audit/envelope-write
   still-completes contract (the module never raises, so the caller's
   audit write can still occur).

KNOWN GAPS on this branch (stated, NOT skipped):
* ``workspace_input_audit.py`` (FAR-801, audit persistence) is NOT merged
  here, so the end-to-end audit-persistence path cannot be exercised in
  this branch. The module-level "detection failure never raises" contract
  is verified instead.
* No MWI retention estimator exists on this branch; the retention-estimator
  test is deferred until the estimator ships.
* The migration test lives in
  ``backend/tests/integration/test_migration_0221_workspace_inputs.py``
  (testcontainers; deferred locally, runs in the deploy-workflow suite).
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine.sandbox_mode import (
    SANDBOX_CAPABILITY_MULTI_HOST,
    SANDBOX_CAPABILITY_WORKSPACE_INPUTS,
    _validate_sandbox_managed_inputs_config,
    derive_sandbox_capabilities,
)
from modulo.core.pipeline_engine.workspace_input_credentials import (
    CloneCredential,
    build_provisioning_credential_scripts,
)
from modulo.core.pipeline_engine.workspace_input_orchestration import (
    ProvisioningError,
    ResolvedInput,
    detect_workspace_input_drift,
    provision_workspace_inputs_in_sandbox,
    resolve_managed_inputs_host_side,
)

_SHA_A = "a" * 40
_SHA_B = "b" * 40
_SHA_C = "c" * 40
_TOKEN_A = "ghp_tokenHostA_0001"
_TOKEN_B = "ghp_tokenHostB_0002"
_URL_A = "https://github.com/org/repo-a.git"
_URL_B = "https://gitlab.com/org/repo-b.git"
_DEST_A = "/home/user/repo-a"
_DEST_B = "/home/user/repo-b"
_CONNECTOR_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_CONNECTOR_B = uuid.UUID("22222222-2222-2222-2222-222222222222")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ls_remote_for(shas_by_ref: dict[str, str]) -> str:
    lines = [f"{sha}\t{ref}\n" for ref, sha in shas_by_ref.items()]
    lines.insert(0, "\n")  # blank header line — ls-remote emits whitespace-afforded output
    return "".join(lines)


def _resolved_input(
    *, url: str = _URL_A, dest: str = _DEST_A, sha: str = _SHA_A, connector_instance_id: Any = None
) -> ResolvedInput:
    return ResolvedInput(
        url=url,
        dest=dest,
        resolved_sha=sha,
        connector_instance_id=connector_instance_id,
    )


def _fake_sandbox() -> MagicMock:
    cmd_result = MagicMock()
    cmd_result.exit_code = 0
    handle = MagicMock()
    handle.wait = AsyncMock(return_value=cmd_result)
    sandbox = MagicMock()
    sandbox.commands.run = AsyncMock(return_value=handle)
    return sandbox


def _sandbox_stdout(stdout: str) -> MagicMock:
    """A sandbox whose commands.run returns a result carrying *stdout*."""
    sandbox = _fake_sandbox()
    result = MagicMock()
    result.stdout = stdout
    sandbox.commands.run = AsyncMock(return_value=result)
    return sandbox


def _collected_scripts(sandbox: MagicMock) -> list[str]:
    scripts: list[str] = []
    for call in sandbox.commands.run.call_args_list:
        args, _kwargs = call
        scripts.append(args[0] if args else "")
    return scripts


class _AsyncCtx:
    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def _fake_session_factory() -> Any:
    session = MagicMock()
    session.begin = MagicMock(return_value=_AsyncCtx(None))
    return MagicMock(return_value=_AsyncCtx(session))


# ---------------------------------------------------------------------------
# 1. Status-transition invariants
# ---------------------------------------------------------------------------


class TestResolutionFailureTransitions:
    async def test_missing_ref_raises_permanent_and_never_returns_partial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        remote = AsyncMock(return_value=_ls_remote_for({"refs/heads/main": _SHA_A}))
        monkeypatch.setattr("modulo.core.pipeline_engine.workspace_input_orchestration._run_git_ls_remote", remote)
        inputs = [
            {"url": _URL_A, "dest": _DEST_A, "ref": {"kind": "branch", "value": "main"}},
            {"url": _URL_A, "dest": _DEST_B, "ref": {"kind": "tag", "value": "v9.9.9-missing"}},
        ]
        with pytest.raises(ProvisioningError) as excinfo:
            await resolve_managed_inputs_host_side(inputs, org_id="org-1")
        assert excinfo.value.error_code == "sandbox.input_resolution_failed"
        assert excinfo.value.retryable is False
        assert "v9.9.9-missing" in str(excinfo.value)
        # Input 1 resolved fine, but the resolution is all-or-nothing: the
        # exception carries nothing partial for the caller to consume.

    async def test_transient_network_error_is_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "modulo.core.pipeline_engine.workspace_input_orchestration._resolve_ref_with_retry",
            AsyncMock(side_effect=TimeoutError("network unreachable")),
        )
        inputs = [{"url": _URL_A, "dest": _DEST_A, "ref": {"kind": "branch", "value": "main"}}]
        with pytest.raises(ProvisioningError) as excinfo:
            await resolve_managed_inputs_host_side(inputs, org_id="org-1")
        assert excinfo.value.retryable is True
        assert excinfo.value.error_code == "sandbox.input_resolution_failed"

    async def test_partial_multi_input_resolution_failure_aborts_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Input 1 resolves, input 2's remote is unreachable after retries:
        the WHOLE resolution raises (transient, retryable) — no partial list."""
        remote = AsyncMock(
            side_effect=[
                _ls_remote_for({"refs/heads/main": _SHA_A}),
                ConnectionError("unreachable"),
                ConnectionError("unreachable"),
                ConnectionError("unreachable"),
            ]
        )
        monkeypatch.setattr("modulo.core.pipeline_engine.workspace_input_orchestration._run_git_ls_remote", remote)
        inputs = [
            {"url": _URL_A, "dest": _DEST_A, "ref": {"kind": "branch", "value": "main"}},
            {"url": _URL_B, "dest": _DEST_B, "ref": {"kind": "branch", "value": "main"}},
        ]
        with pytest.raises(ProvisioningError) as excinfo:
            await resolve_managed_inputs_host_side(inputs, org_id="org-1", max_retries=2)
        assert excinfo.value.retryable is True


class TestCheckoutAndPartialProvisioning:
    async def test_completed_provision_means_every_input_cloned_with_its_resolved_sha(self) -> None:
        """provisioned implies checked out: after a successful provision, every
        input received a clone script that checks out ITS resolved SHA."""
        sandbox = _fake_sandbox()
        resolved = [
            _resolved_input(url=_URL_A, dest=_DEST_A, sha=_SHA_A),
            _resolved_input(url=_URL_B, dest=_DEST_B, sha=_SHA_B),
        ]
        await provision_workspace_inputs_in_sandbox(sandbox, resolved)

        scripts = _collected_scripts(sandbox)
        assert len(scripts) == 2  # one clone per input; public repos have no credential scripts
        for script, inp in zip(scripts, resolved, strict=True):
            assert f"checkout {inp.resolved_sha}" in script
            assert inp.dest in script
        # Ordered: the first input's clone ran before the second's.
        first = next(i for i, s in enumerate(scripts) if _DEST_A in s)
        second = next(i for i, s in enumerate(scripts) if _DEST_B in s)
        assert first < second

    async def test_partial_multi_input_clone_failure_short_circuits_later_inputs(self) -> None:
        """Input 2's clone failure raises (checkout-failed code); input 3 is
        never attempted and the agent command can never run on partial
        provisioning."""
        calls: list[str] = []

        async def run(script: str, timeout: float) -> Any:  # noqa: ASYNC109 - matches sandbox SDK shape
            calls.append(script)
            if _DEST_B in script:
                raise RuntimeError("git clone died")
            return MagicMock()

        sandbox = _fake_sandbox()
        sandbox.commands.run = run  # type: ignore[method-assign]
        resolved = [
            _resolved_input(url=_URL_A, dest=_DEST_A, sha=_SHA_A),
            _resolved_input(url=_URL_B, dest=_DEST_B, sha=_SHA_B),
            _resolved_input(url=_URL_A, dest="/home/user/repo-c", sha=_SHA_C),
        ]
        with pytest.raises(ProvisioningError) as excinfo:
            await provision_workspace_inputs_in_sandbox(sandbox, resolved)
        assert excinfo.value.error_code == "sandbox.input_checkout_failed"
        assert _DEST_B in str(excinfo.value)
        assert not any("/home/user/repo-c" in s for s in calls)
        assert any(_DEST_A in s for s in calls)

    def test_resolved_inputs_are_frozen_checkouts(self) -> None:
        """Between resolve and clone, the persisted resolved SHA cannot be
        mutated (a crash or force-push between the two steps cannot drift
        the checkout — the artefact is frozen data, not a live session)."""
        inp = _resolved_input(sha=_SHA_A)
        with pytest.raises(AttributeError):
            inp.resolved_sha = "deadbeef"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2. Multi-host token isolation
# ---------------------------------------------------------------------------


class TestMultiHostTokenIsolation:
    def test_host_scripts_contain_only_their_own_token(self) -> None:
        cred_a = CloneCredential(kind="token", host="github.com", username="x-access-token", secret=_TOKEN_A)
        cred_b = CloneCredential(kind="token", host="gitlab.com", username="oauth2", secret=_TOKEN_B)
        setup_a, _ = build_provisioning_credential_scripts(cred=cred_a, host="github.com")
        setup_b, _ = build_provisioning_credential_scripts(cred=cred_b, host="gitlab.com")
        assert _TOKEN_A in setup_a
        assert _TOKEN_A not in setup_b
        assert _TOKEN_B in setup_b
        assert _TOKEN_B not in setup_a

    def test_public_repo_gets_no_credential_material(self) -> None:
        setup, teardown = build_provisioning_credential_scripts(cred=None, host="github.com")
        assert not setup
        assert not teardown
        assert "GIT_ASKPASS" not in setup

    async def test_resolution_carries_per_host_credentials_only_on_their_own_input(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token_by_connector = {_CONNECTOR_A: _TOKEN_A, _CONNECTOR_B: _TOKEN_B}

        async def _fake_resolve(session: Any, *, connector_instance_id: Any, host: str) -> CloneCredential:
            token = token_by_connector[connector_instance_id]
            return CloneCredential(kind="token", host=host, username="x-access-token", secret=token)

        remote = AsyncMock(
            side_effect=lambda url: _ls_remote_for({"refs/heads/main": _SHA_A if url == _URL_A else _SHA_B})
        )
        monkeypatch.setattr("modulo.core.pipeline_engine.workspace_input_orchestration._run_git_ls_remote", remote)
        monkeypatch.setattr(
            "modulo.core.pipeline_engine.workspace_input_credentials.resolve_clone_credential",
            _fake_resolve,
        )
        resolved = await resolve_managed_inputs_host_side(
            [
                {
                    "url": _URL_A,
                    "dest": _DEST_A,
                    "ref": {"kind": "branch", "value": "main"},
                    "connector_instance_id": str(_CONNECTOR_A),
                },
                {
                    "url": _URL_B,
                    "dest": _DEST_B,
                    "ref": {"kind": "branch", "value": "main"},
                    "connector_instance_id": str(_CONNECTOR_B),
                },
            ],
            org_id="org-1",
            session_factory=_fake_session_factory(),
        )

        setup_a = resolved[0].credential_setup_script
        setup_b = resolved[1].credential_setup_script
        assert _TOKEN_A in setup_a
        assert _TOKEN_A not in setup_b
        assert _TOKEN_B in setup_b
        assert _TOKEN_B not in setup_a

        # A public-repo (unknown host, no connector) input gets no credential.
        public = await resolve_managed_inputs_host_side(
            [{"url": _URL_A, "dest": _DEST_B, "ref": {"kind": "branch", "value": "main"}}],
            org_id="org-1",
        )
        assert not public[0].credential_setup_script


# ---------------------------------------------------------------------------
# 3. Adversarial dest containment
# ---------------------------------------------------------------------------


def _node_with_dest(dest: Any) -> dict[str, Any]:
    return {
        "id": "n1",
        "workspace_inputs": [{"dest": dest, "url": "https://github.com/org/repo.git", "ref": {"kind": "branch"}}],
    }


class TestAdversarialDestContainment:
    @pytest.mark.parametrize(
        "dest",
        [
            pytest.param(None, id="null"),
            pytest.param("", id="empty"),
            pytest.param("   ", id="blank"),
            pytest.param(".", id="dot"),
            pytest.param("..", id="parent"),
            pytest.param("../etc", id="relative-parent"),
            pytest.param("/tmp/evil", id="outside-home"),
            pytest.param("/home/user/../../tmp", id="traversal-up"),
            pytest.param("/home/user/../other", id="traversal-sibling"),
            pytest.param("/home/user", id="home-itself"),
            pytest.param("/home/user/.git", id="home-dotgit"),
            pytest.param("/home/user/repo/.git", id="repo-dotgit"),
            pytest.param("repo/.git/config", id="relative-repo-dotgit-config"),
            pytest.param("/home/user/agent.log", id="agent-log"),
            pytest.param("/home/user/output.json", id="output-json"),
            pytest.param("/home/user/.gitconfig", id="gitconfig"),
            pytest.param("/home/user/.ssh", id="ssh"),
            pytest.param("/home/user/repo/.git-policy", id="denylist-component"),
            pytest.param("/home/user/" + "/".join(f"d{i}" for i in range(9)), id="depth-abuse"),
        ],
    )
    def test_adversarial_dests_rejected(self, dest: Any) -> None:
        with pytest.raises(ValueError, match="workspace_inputs"):
            _validate_sandbox_managed_inputs_config(_node_with_dest(dest))

    @pytest.mark.parametrize(
        "dest",
        ["repo", "/home/user/repo", "/home/user/deep/nested/repo", "  /home/user/repo  "],
    )
    def test_benign_dests_accepted(self, dest: str) -> None:
        assert _validate_sandbox_managed_inputs_config(_node_with_dest(dest)) is None

    def test_opt_in_absent_config_is_valid(self) -> None:
        """Workspace inputs are OPT-IN: a node without them validates cleanly."""
        assert _validate_sandbox_managed_inputs_config({"id": "n1"}) is None
        assert _validate_sandbox_managed_inputs_config({"id": "n1", "workspace_inputs": []}) is None


# ---------------------------------------------------------------------------
# 4. multi_host BOOLEAN capability conformance (FAR-798)
# ---------------------------------------------------------------------------


class TestMultiHostCapabilityConformance:
    def test_capability_constants_registered(self) -> None:
        assert SANDBOX_CAPABILITY_MULTI_HOST == "sandbox.git_credentials.multi_host"
        assert SANDBOX_CAPABILITY_WORKSPACE_INPUTS == "sandbox.workspace_inputs"

    def test_single_host_is_false(self) -> None:
        caps = derive_sandbox_capabilities({"node_type": "sandbox_agent", "allowed_hosts": {"github.com": "T"}})
        assert caps[SANDBOX_CAPABILITY_MULTI_HOST] is False

    def test_two_hosts_is_true(self) -> None:
        caps = derive_sandbox_capabilities(
            {"node_type": "sandbox_agent", "allowed_hosts": {"github.com": "TA", "gitlab.com": "TB"}}
        )
        assert caps[SANDBOX_CAPABILITY_MULTI_HOST] is True

    def test_absent_or_none_is_false(self) -> None:
        for node_def in ({"node_type": "sandbox_agent"}, {"node_type": "sandbox_agent", "allowed_hosts": None}):
            caps = derive_sandbox_capabilities(node_def)
            assert caps[SANDBOX_CAPABILITY_MULTI_HOST] is False

    def test_empty_dict_is_false(self) -> None:
        caps = derive_sandbox_capabilities({"node_type": "sandbox_agent", "allowed_hosts": {}})
        assert caps[SANDBOX_CAPABILITY_MULTI_HOST] is False

    def test_smuggled_shapes_fail_closed(self) -> None:
        caps = derive_sandbox_capabilities(
            {"node_type": "sandbox_agent", "allowed_hosts": {"github.com": "TA", "gitlab.com": 42}}
        )
        assert caps[SANDBOX_CAPABILITY_MULTI_HOST] is None
        caps = derive_sandbox_capabilities({"node_type": "sandbox_agent", "allowed_hosts": "github.com"})
        assert caps[SANDBOX_CAPABILITY_MULTI_HOST] is None

    def test_capability_values_stay_bool_or_none(self) -> None:
        """ADR 033 contract: every capability value is bool | None, never a set."""
        caps = derive_sandbox_capabilities(
            {"node_type": "sandbox_agent", "allowed_hosts": {"a.com": "A", "b.com": "B"}}
        )
        for value in caps.values():
            assert isinstance(value, (bool, type(None)))


# ---------------------------------------------------------------------------
# 5. Deterministic stdout SHA extraction fixture
# ---------------------------------------------------------------------------


class TestStdoutShaExtractionFixture:
    async def test_trailing_log_noise_does_not_break_extraction(self) -> None:
        """Realistic multi-line post-agent output: rev-parse HEAD on the first
        line, followed by trailing log/warning noise — the SHA is extracted
        from the head and matches the resolved SHA (no drift)."""
        stdout = (
            f"{_SHA_A}\n"  # the rev-parse result line
            "warning: branch 'main' has not been rebased\n"  # git noise
            "nessus: no root metapackage\n"  # env log noise
        )
        sandbox = _sandbox_stdout(stdout)
        drift = await detect_workspace_input_drift(sandbox, [_resolved_input(url=_URL_A, dest=_DEST_A, sha=_SHA_A)])
        assert len(drift) == 1
        assert drift[0].final_sha == _SHA_A
        assert drift[0].drift_detected is False

    async def test_drifted_head_with_trailing_noise_reports_drift(self) -> None:
        sandbox = _sandbox_stdout(_SHA_B + "\nnoise line two\n")
        drift = await detect_workspace_input_drift(sandbox, [_resolved_input(url=_URL_A, dest=_DEST_A, sha=_SHA_A)])
        assert drift[0].final_sha == _SHA_B
        assert drift[0].drift_detected is True

    async def test_leading_noise_fails_closed_as_drift(self) -> None:
        """Leading noise on stdout parses as the \"SHA\" — the module fails
        CLOSED (drift_detected=True, never a silent false-negative)."""
        stdout = f"log noise before the sha\n{_SHA_A}\n"
        sandbox = _sandbox_stdout(stdout)
        drift = await detect_workspace_input_drift(sandbox, [_resolved_input(url=_URL_A, dest=_DEST_A, sha=_SHA_A)])
        assert drift[0].final_sha == "log noise before the sha"
        assert drift[0].drift_detected is True

    async def test_empty_stdout_reports_drift(self) -> None:
        sandbox = _sandbox_stdout("")
        drift = await detect_workspace_input_drift(sandbox, [_resolved_input(url=_URL_A, dest=_DEST_A, sha=_SHA_A)])
        assert not drift[0].final_sha
        assert drift[0].drift_detected is True


# ---------------------------------------------------------------------------
# 6. Drift-check failure never blocks the audit/envelope write
# ---------------------------------------------------------------------------


class TestDriftFailureStillCompletes:
    async def test_drift_check_failure_does_not_raise_and_reports_fail_closed(self) -> None:
        """The drift check failing itself MUST NOT raise (so the caller's
        audit/envelope write still occurs) and reports UNKNOWN drift
        fail-closed per input."""
        sandbox = _fake_sandbox()
        sandbox.commands.run = AsyncMock(side_effect=RuntimeError("sandbox died"))

        drift = await detect_workspace_input_drift(
            sandbox,
            [_resolved_input(url=_URL_A, dest=_DEST_A, sha=_SHA_A), _resolved_input(dest=_DEST_B, sha=_SHA_B)],
        )
        assert len(drift) == 2
        for result in drift:
            assert result.drift_detected is True
            assert not result.final_sha

    async def test_healthy_input_still_reports_no_drift_after_a_failed_peer(self) -> None:
        one_failed = AsyncMock(side_effect=RuntimeError("probe died"))
        sandbox_failed = _fake_sandbox()
        sandbox_failed.commands.run = one_failed
        drift = await detect_workspace_input_drift(
            sandbox_failed, [_resolved_input(dest="/home/user/repo-c", sha=_SHA_C)]
        )
        assert drift[0].drift_detected is True

        # A healthy reader on its own input still reports no drift:
        sandbox_ok = _sandbox_stdout(_SHA_A)
        drift = await detect_workspace_input_drift(sandbox_ok, [_resolved_input(url=_URL_A, dest=_DEST_A, sha=_SHA_A)])
        assert drift[0].drift_detected is False

    async def test_node_envelope_still_written_when_drift_check_raises(self) -> None:
        """End-to-end: a raising drift check maps to a node that still
        completes with its audit/envelope fields written (degraded, fail-open
        with a log) — never an opaque node crash after successful work."""
        from modulo.core.pipeline_engine.node_runner import make_sandbox_agent_fn

        node_def: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "node_type": "sandbox_agent",
            "position": {"x": 0, "y": 0},
            "template_id": "opencode",
            "mode": "script",
            "script_command": "python3 /home/user/main.py",
            "agent_id": str(uuid.uuid4()),
            "workspace_inputs": [{"url": _URL_A, "dest": _DEST_A, "ref": {"kind": "branch", "value": "main"}}],
        }
        cmd_result = MagicMock()
        cmd_result.exit_code = 0
        cmd_result.stdout = "out"
        cmd_result.stderr = ""
        handle = MagicMock()
        handle.wait = AsyncMock(return_value=cmd_result)
        sandbox = MagicMock()
        sandbox.files.write = AsyncMock()
        sandbox.files.read = AsyncMock(return_value='{"result": "ok"}')
        sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=0))
        sandbox.commands.run = AsyncMock(return_value=handle)
        sandbox.kill = AsyncMock()
        sandbox.get_metrics = AsyncMock(return_value=MagicMock(cpu_used_pct=1.0, mem_used=1, disk_used=1))
        state: dict[str, Any] = {
            "run_context": {"input": {"task": "x"}},
            "_run_id": str(uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")),
            "_pipeline_id": "pipe-1",
            "_org_id": str(uuid.UUID("11111111-2222-3333-4444-555555555555")),
        }
        with (
            patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
            patch("modulo.core.runner_bindings.resolve_agent_bindings", new=AsyncMock(return_value={})),
            patch(
                "modulo.core.pipeline_engine.workspace_input_orchestration.resolve_managed_inputs_host_side",
                new=AsyncMock(return_value=[_resolved_input()]),
            ),
            patch(
                "modulo.core.pipeline_engine.workspace_input_orchestration.provision_workspace_inputs_in_sandbox",
                new=AsyncMock(),
            ),
            patch(
                "modulo.core.pipeline_engine.workspace_input_orchestration.detect_workspace_input_drift",
                new=AsyncMock(side_effect=TimeoutError()),
            ),
            patch(
                "modulo.settings.get_settings",
                new=MagicMock(return_value=MagicMock(modulo_workspace_inputs_enabled=True)),
            ),
        ):
            result = await make_sandbox_agent_fn(node_def)(state)
        assert result["output"]["status"] == "completed"
        # The check raised, so the envelope carries NO drift claim (_UNSET keys
        # are omissive) — but the node still completed and the rest of the
        # audit fields (agent_stdout, exit path, attempt_key) were written:
        assert "workspace_drift" not in result["output"]
        assert "workspace_drift_detected" not in result["output"]
        assert result["output"]["agent_stdout"] == "out"


# ---------------------------------------------------------------------------
# Deferred (stated gaps — see module docstring):
# * audit persistence (FAR-801 sibling branch) — module not merged here
# * MWI retention estimator — module does not exist on this branch
# * migration 0221 — backend/tests/integration/test_migration_0221_workspace_inputs.py
# ---------------------------------------------------------------------------
