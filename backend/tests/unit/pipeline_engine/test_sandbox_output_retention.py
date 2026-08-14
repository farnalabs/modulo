"""Regression tests for FAR-188: raw sandbox output retention on parse failure.

When a sandbox_agent node's ``/home/user/output.json`` is missing, empty,
malformed, or parses to a NON-dict, the node raises ``SandboxNodeFailedError``
(retryable, A6) — but the run record must STILL retain the RAW output (the file
content, the captured stdout, or both) so a ``pr_url`` the agent created inside
the sandbox is never lost when the JSON fails to parse (classification FAR-189
depends on it).

The invariant under test: a run that created a PR must never lose the evidence
of that PR when output.json fails to parse.

QA round 1 rework (FAR-188): retention lives in a DEDICATED column
``runs.raw_output_markers`` keyed by ``attempt_key`` — NEVER in the Agent
Return Contract columns (``outputs_json`` / ``node_telemetry_json``), so the
node-output endpoint can never serve raw stdout, ``recover_node``'s
already-completed guard never sees a fake completed node, and finalize's
split-output machinery never touches the marker. The persist is bounded by
``asyncio.wait_for`` so a hung DB fails open with a log instead of converting
the retryable raise into a terminal ``node_timeout``.
"""

import asyncio
import logging
import re
import uuid
from typing import Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine.node_runner import (
    SandboxNodeFailedError,
    _persist_raw_output_marker,
    make_sandbox_agent_fn,
)

_ORG_ID = str(uuid.UUID("11111111-2222-3333-4444-555555555555"))
_AGENT_COMMAND = "opencode run --auto --format json < /home/user/prompt.md"
# Fail-open attempt key derived when the test state carries no claim lease:
# ``run:{run_id}:node:{node_id}:claim-unknown``.
_ATTEMPT_KEY = "run:run-1:node:n1:claim-unknown"
_PR_1 = "https://github.com/farnalabs/modulo/pull/1"
_PR_456 = "https://github.com/farnalabs/modulo/pull/456"
_PR_777 = "https://github.com/farnalabs/modulo/pull/777"
_PR_999 = "https://github.com/farnalabs/modulo/pull/999"


def _base_node_def(**overrides) -> dict:
    node_def = {
        "id": "n1",
        "agent_prompt": "Do the thing",
        "agent_command": _AGENT_COMMAND,
    }
    node_def.update(overrides)
    return node_def


def _run_state() -> dict:
    return {
        "run_context": {"input": {"task": "x"}},
        "_run_id": "run-1",
        "_pipeline_id": "pipe-1",
        "_org_id": _ORG_ID,
    }


def _make_sandbox_mock(*, output_json: str = '{"summary": "done"}', log_content: str = ""):
    cmd_result = MagicMock()
    cmd_result.exit_code = 0
    cmd_result.stdout = "agent stdout"
    cmd_result.stderr = ""

    handle = MagicMock()
    handle.wait = AsyncMock(return_value=cmd_result)

    def _read(path, format="text", **kwargs):
        if str(path).endswith("output.json"):
            return output_json
        return log_content

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.files.read = AsyncMock(side_effect=_read)
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=len(log_content)))
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()
    return sandbox


class _FakeRunRow:
    """In-memory ``runs`` row capturing the persisted retention column.

    Mirrors the ORM ``Run`` surface the persist helper touches. The Agent
    Return Contract columns (``outputs_json`` / ``node_telemetry_json``) are
    present so tests can PROVE retention no longer leaks into them (FIX 1).
    """

    def __init__(self, run_id: str = "run-1") -> None:
        self.id = run_id
        self.outputs_json: dict | None = None
        self.node_telemetry_json: dict | None = None
        self.raw_output_markers: dict | None = None


class _RetentionResult:
    def __init__(self, row: _FakeRunRow | None) -> None:
        self._row = row

    def scalar_one_or_none(self) -> _FakeRunRow | None:
        return self._row


class _RetentionSession:
    """Fake async session serving the ORM ``select(Run)`` and ORM writes.

    Compatible with the REAL ``set_rls_org`` (deliberately NOT mocked away —
    FIX 9): it reports an active transaction and a sqlite dialect, so the real
    guard stores the org in ``session.info`` exactly as on generic backends. A
    session constructed with ``row=None`` models an RLS-hidden foreign-org run
    (the SELECT resolves to no row).
    """

    def __init__(self, row: _FakeRunRow | None) -> None:
        self._row = row
        self.info: dict = {}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> "_RetentionSession":
        return self

    def in_transaction(self) -> bool:
        return True

    async def get_bind(self) -> MagicMock:
        bind = MagicMock()
        bind.dialect.name = "sqlite"
        return bind

    async def execute(self, stmt: object, params: dict | None = None) -> _RetentionResult:
        if "FROM runs" in str(stmt):
            return _RetentionResult(self._row)
        return _RetentionResult(None)

    async def flush(self) -> None:
        return None


def _retention_env(output_json: str):
    """Return (node_fn, fake_row, sandbox) wired to an in-memory run row."""
    row = _FakeRunRow()

    def _factory() -> _RetentionSession:
        return _RetentionSession(row)

    node_def = _base_node_def(timeout_seconds=30)
    fn = make_sandbox_agent_fn(node_def, session_factory=_factory)
    sandbox = _make_sandbox_mock(output_json=output_json)
    return fn, row, sandbox


def _single_marker(row: _FakeRunRow) -> dict:
    assert row.raw_output_markers is not None, "raw output must be retained on the run record"
    assert len(row.raw_output_markers) == 1
    return next(iter(row.raw_output_markers.values()))


async def test_malformed_output_json_retains_raw_output_and_pr_url():
    """A malformed output.json (read succeeds, json.loads fails) still leaves the
    RAW content — including an embedded pr_url — on the run record, in the
    dedicated column only (never in the Agent Return Contract columns)."""
    malformed = '{"summary": "PR created", "pr_url": "https://github.com/farnalabs/modulo/pull/123", '
    fn, row, sandbox = _retention_env(output_json=malformed)

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    marker = _single_marker(row)
    assert marker["status"] == "failed"
    assert marker["parse_error"]
    assert "JSONDecodeError" in marker["parse_error"]
    assert "https://github.com/farnalabs/modulo/pull/123" in marker["raw_output"]
    assert marker["pr_url"] == "https://github.com/farnalabs/modulo/pull/123"
    assert marker["_modulo_marker"] is True
    assert marker["attempt_key"] == _ATTEMPT_KEY
    # FIX 1(b): the Agent Return Contract columns stay clean — the node-output
    # endpoint can never serve raw stdout.
    assert row.outputs_json is None
    assert row.node_telemetry_json is None


async def test_bytes_output_json_that_fails_json_loads_retains_raw():
    """A read that returns bytes which fail json.loads is decoded and retained."""
    raw_bytes = b'{"pr_url": "https://github.com/farnalabs/modulo/pull/456" '
    fn, row, sandbox = _retention_env(output_json=raw_bytes)

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    marker = _single_marker(row)
    assert isinstance(marker["raw_output"], str)
    assert "https://github.com/farnalabs/modulo/pull/456" in marker["raw_output"]
    assert marker["pr_url"] == _PR_456


async def test_missing_output_json_falls_back_to_captured_stdout():
    """When the output.json read itself fails (file missing / unreadable), the
    captured stdout that carried the agent's result is retained instead."""
    sandbox = _make_sandbox_mock()

    # The final read of /home/user/output.json raises; the drain-log reads of
    # /home/user/agent.log still succeed so the watchdog stays alive.
    def _read(path, format="text", **kwargs):
        if str(path).endswith("output.json"):
            raise OSError("output.json missing")
        return ""

    sandbox.files.read = AsyncMock(side_effect=_read)
    row = _FakeRunRow()

    def _factory() -> _RetentionSession:
        return _RetentionSession(row)

    fn = make_sandbox_agent_fn(_base_node_def(timeout_seconds=30), session_factory=_factory)

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    marker = _single_marker(row)
    assert marker["status"] == "failed"
    assert marker["parse_error"]
    assert "OSError" in marker["parse_error"]
    # The stdout carried the raw content (here the fallback source).
    assert "agent stdout" in marker["raw_output"]


async def test_persist_failure_never_blocks_the_raise():
    """A failing DB write during retention must NOT block the SandboxNodeFailedError
    raise — the parse-failure path is best-effort and must not block terminalization."""
    _fn, row, sandbox = _retention_env(output_json='{"summary": "broken", ')

    class _BoomSession(_RetentionSession):
        async def execute(self, stmt: object, params: dict | None = None) -> _RetentionResult:
            raise RuntimeError("db down")

    def _boom_factory() -> _BoomSession:
        return _BoomSession(row)

    boom_fn = make_sandbox_agent_fn(_base_node_def(timeout_seconds=30), session_factory=_boom_factory)

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxNodeFailedError, match=r"no parseable output\.json"),
    ):
        await boom_fn(_run_state())


async def test_stalled_command_retains_captured_stdout():
    """A stalled command (idle watchdog fires, cmd_result None) retains the
    drained stdout as the raw evidence — a pr_url echoed before the stall must
    survive (FAR-188 invariant).

    The watchdog's kill must actually be awaited (handle.kill is an AsyncMock
    and is asserted called): the OLD test passed for the wrong reason — a
    non-awaitable kill raising inside the watchdog's try/except let it go green
    with EMPTY raw_output."""
    cmd_result = MagicMock()
    cmd_result.exit_code = -1

    handle = MagicMock()
    handle.wait = AsyncMock(side_effect=asyncio.TimeoutError)
    handle.kill = AsyncMock()

    log_content = f"agent working...\nPR created: {_PR_999}\n"

    def _read(path, format="text", **kwargs):
        if str(path).endswith("output.json"):
            raise OSError("no output.json")
        return log_content

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.files.read = AsyncMock(side_effect=_read)
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=len(log_content)))
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()

    row = _FakeRunRow()

    def _factory() -> _RetentionSession:
        return _RetentionSession(row)

    fn = make_sandbox_agent_fn(_base_node_def(timeout_seconds=30), session_factory=_factory)

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.core.pipeline_engine.node_runner._SANDBOX_IDLE_TIMEOUT", 0.0),
        patch("modulo.core.pipeline_engine.node_runner._SANDBOX_TAIL_INTERVAL", 0.01),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    handle.kill.assert_awaited_once()
    marker = _single_marker(row)
    assert marker["status"] == "failed"
    assert marker["parse_error"]
    assert log_content in marker["raw_output"]
    assert marker["pr_url"] == _PR_999


async def test_successful_parse_does_not_write_retention_marker():
    """A clean output.json parse does NOT write a raw-output marker — retention is
    only for the parse-failure path."""
    fn, row, sandbox = _retention_env(output_json='{"summary": "done", "pr_url": ""}')

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    assert row.raw_output_markers is None
    assert row.outputs_json is None
    assert row.node_telemetry_json is None


async def test_multi_attempt_retention_preserves_first_pr_url():
    """FIX 2 (attempt_key keying): two attempts under DIFFERENT attempt keys both
    survive (a retry no longer clobbers the previous attempt's evidence), and
    re-persisting the SAME attempt key with an empty pr_url never wipes
    attempt-1's pr_url."""
    row = _FakeRunRow()

    def _factory() -> _RetentionSession:
        return _RetentionSession(row)

    attempt_1 = "run:run-1:node:n1:1"
    attempt_2 = "run:run-1:node:n1:2"

    def _marker(pr_url: str) -> dict:
        return {
            "status": "failed",
            "summary": "s",
            "raw_output": "raw",
            "pr_url": pr_url,
            "_modulo_marker": True,
        }

    # Attempt 1 persists with a pr_url.
    await _persist_raw_output_marker(
        _factory, run_id="run-1", org_id_raw=_ORG_ID, node_id="n1", attempt_key=attempt_1, marker=_marker(_PR_1)
    )
    # A retry re-executes the same node under the SAME attempt key with an empty pr_url.
    await _persist_raw_output_marker(
        _factory, run_id="run-1", org_id_raw=_ORG_ID, node_id="n1", attempt_key=attempt_1, marker=_marker("")
    )
    # A DIFFERENT attempt key is a separate attempt — its marker survives alongside.
    await _persist_raw_output_marker(
        _factory, run_id="run-1", org_id_raw=_ORG_ID, node_id="n1", attempt_key=attempt_2, marker=_marker("")
    )

    assert row.raw_output_markers is not None
    assert set(row.raw_output_markers) == {attempt_1, attempt_2}, "every attempt's evidence survives"
    assert row.raw_output_markers[attempt_1]["pr_url"] == _PR_1, "attempt-1 pr_url is preserved, never wiped"


async def test_db_hang_persist_fails_open_and_node_stays_retryable(caplog):
    """FIX 3 (bounded persist): a hung DB write (execute never resolves) must fail
    open with a log BEFORE the node decorator grace budget is consumed — the
    node still raises the retryable SandboxNodeFailedError and never becomes a
    terminal node_timeout."""
    caplog.set_level(logging.WARNING, logger="modulo.core.pipeline_engine.node_runner")
    _fn, row, sandbox = _retention_env(output_json='{"summary": "broken", ')

    class _HangSession(_RetentionSession):
        async def execute(self, stmt: object, params: dict | None = None) -> _RetentionResult:
            await asyncio.Event().wait()  # never resolves
            return _RetentionResult(None)

    def _hang_factory() -> _HangSession:
        return _HangSession(row)

    hang_fn = make_sandbox_agent_fn(_base_node_def(timeout_seconds=30), session_factory=_hang_factory)

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.core.pipeline_engine.node_runner._RAW_OUTPUT_MARKER_PERSIST_TIMEOUT", 0.1),
        pytest.raises(SandboxNodeFailedError, match=r"no parseable output\.json"),
    ):
        await hang_fn(_run_state())

    assert row.raw_output_markers is None, "the hung write must not land"
    assert any("raw_output_marker_persist_timeout_or_error" in r.message for r in caplog.records), (
        "the timeout must be logged (fail open), not silently swallowed"
    )


@pytest.mark.parametrize("non_dict", ["[]", '"just a string"', "123"])
async def test_non_dict_output_json_retains_marker_and_completes(non_dict):
    """Corrected FIX 4: a PARSEABLE but non-dict output.json ([], "str", 123)
    retains the raw evidence as a marker but does NOT raise — it continues
    through the existing shaping path with agent_status None (the pre-existing
    test_node_runner_agent_status suite asserts exactly this proceed-with-None
    behaviour and must stay green)."""
    fn, row, sandbox = _retention_env(output_json=non_dict)

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    marker = _single_marker(row)
    assert marker["status"] == "failed"
    assert "non-dict" in marker["parse_error"]
    assert non_dict in marker["raw_output"]
    assert marker["_modulo_marker"] is True
    # The node did NOT raise: it completed via the existing path, and no
    # agent_status is fabricated from the non-dict output.
    assert result["output"]["agent_status"] is None
    assert result["artifacts"][0]["output"]["agent_status"] is None


async def test_json_null_output_retains_and_raises():
    """``null`` parses to None — the TRULY-no-output case: the raw marker is
    retained AND the node raises SandboxNodeFailedError (unchanged original
    FAR-188 semantics)."""
    fn, row, sandbox = _retention_env(output_json="null")

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxNodeFailedError, match=r"no parseable output\.json"),
    ):
        await fn(_run_state())

    marker = _single_marker(row)
    assert "null" in marker["raw_output"]
    assert marker["_modulo_marker"] is True


async def test_non_dict_output_with_stdout_pr_url_extracts_and_completes():
    """Corrected FIX 4: pr_url extraction works in the non-dict continue path
    too — a pr_url echoed to stdout when output.json parses to a non-dict value
    is captured in the marker, and the node still completes (no raise)."""
    log_content = f"Agent finished. PR: {_PR_777}\n"
    sandbox = _make_sandbox_mock(output_json="[]", log_content=log_content)
    row = _FakeRunRow()

    def _factory() -> _RetentionSession:
        return _RetentionSession(row)

    fn = make_sandbox_agent_fn(_base_node_def(timeout_seconds=30), session_factory=_factory)

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    marker = _single_marker(row)
    assert marker["pr_url"] == _PR_777
    assert _PR_777 in marker["raw_output"]
    assert result["output"]["agent_status"] is None


async def test_pr_url_echoed_to_stdout_found_when_output_json_malformed():
    """FIX 5 (union of raw sources): the retained evidence is the file content AND
    the captured stdout — a pr_url echoed to stdout when output.json is
    present-but-malformed (a naive ``raw_output or stdout`` fallback would drop
    it) must still be found."""
    malformed = '{"summary": "PR created but output truncated'  # json.loads fails
    log_content = f"Agent finished. PR: {_PR_777}\n"
    sandbox = _make_sandbox_mock(output_json=malformed, log_content=log_content)
    row = _FakeRunRow()

    def _factory() -> _RetentionSession:
        return _RetentionSession(row)

    fn = make_sandbox_agent_fn(_base_node_def(timeout_seconds=30), session_factory=_factory)

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    marker = _single_marker(row)
    assert marker["pr_url"] == _PR_777
    assert _PR_777 in marker["raw_output"]


async def test_cross_tenant_run_id_writes_nothing(caplog):
    """FIX 9 (cross-tenant isolation): persisting a marker for a run_id the
    org-scoped query cannot see (RLS-invisible / foreign org) must write NOTHING
    and never raise. The REAL ``set_rls_org`` guard is exercised (not mocked
    away); the fake session resolves the SELECT to no row exactly as RLS would
    hide a foreign-org run."""
    caplog.set_level(logging.WARNING, logger="modulo.core.pipeline_engine.node_runner")
    row = _FakeRunRow()

    def _foreign_factory() -> _RetentionSession:
        return _RetentionSession(None)  # SELECT returns no row — RLS-hidden

    await _persist_raw_output_marker(
        _foreign_factory,
        run_id="run-foreign-org",
        org_id_raw=_ORG_ID,
        node_id="n1",
        attempt_key="run:run-foreign-org:node:n1:0",
        marker={"status": "failed", "summary": "s", "raw_output": "x", "_modulo_marker": True},
    )

    assert row.raw_output_markers is None, "no marker may be written for an RLS-invisible run"
    assert any("raw_output_marker_skip_row_not_found" in r.message for r in caplog.records), (
        "the RLS-hidden path must log a distinguishable WARNING"
    )


async def test_silent_early_returns_are_logged(caplog):
    """FIX 8 (observability): silent early returns — no run_id and an unparseable
    org id — log WARNINGs instead of silently skipping."""
    caplog.set_level(logging.WARNING, logger="modulo.core.pipeline_engine.node_runner")
    row = _FakeRunRow()

    def _factory() -> _RetentionSession:
        return _RetentionSession(row)

    await _persist_raw_output_marker(
        _factory, run_id="", org_id_raw=_ORG_ID, node_id="n1", attempt_key=None, marker={"x": 1}
    )
    await _persist_raw_output_marker(
        _factory, run_id="run-1", org_id_raw="not-a-uuid", node_id="n1", attempt_key=None, marker={"x": 1}
    )

    messages = {r.message for r in caplog.records}
    assert "sandbox_agent.raw_output_marker_skip_no_run_id" in messages
    assert "sandbox_agent.raw_output_marker_skip_unparseable_org" in messages


async def test_raw_output_redacts_tokenized_git_urls_but_keeps_pr_url():
    """FAR-188 QA round 2: a malformed output.json whose raw source embeds a
    tokenized git URL (the sandbox runs with GITHUB_TOKEN injected; agent
    git push/clone output embeds ``https://x-access-token:<PAT>@github.com/...``)
    is persisted with the credential scrubbed — while pr_url extraction still
    runs against the FULL UNREDACTED source so the evidence is never lost."""
    malformed = (
        '{"summary": "clone via tokenized url", '
        '"log": "https://x-access-token:ghp_abc123@github.com/org/repo.git and '
        'http://x-access-token:ghp_http456@github.com/org/repo2.git", '
        '"pr_url": "https://github.com/org/repo/pull/42"'
    )
    fn, row, sandbox = _retention_env(output_json=malformed)

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    marker = _single_marker(row)
    assert "ghp_abc123" not in marker["raw_output"], "the https tokenized URL userinfo must be redacted"
    assert "ghp_http456" not in marker["raw_output"], "the http tokenized URL userinfo must be redacted"
    assert "https://<redacted>@github.com/" in marker["raw_output"]
    assert "http://<redacted>@github.com/" in marker["raw_output"]
    assert "x-access-token:" not in marker["raw_output"]
    # pr_url is extracted from the FULL source BEFORE redaction — never lost.
    assert marker["pr_url"] == "https://github.com/org/repo/pull/42"


async def test_raw_output_redacts_bare_token_patterns():
    """FAR-188 QA round 2: defensively mask bare credential values that follow
    known labels (ghp_/gho_/github_pat_, Bearer , token=, x-access-token:) even
    when they appear OUTSIDE a URL — e.g. echoed auth lines in agent stdout."""
    log_content = (
        f"clone ok\nauth: Bearer gho_secret789\ntoken=github_pat_11AAABBBCC\nx-access-token:ghp_zzzzzz\nPR: {_PR_456}\n"
    )
    sandbox = _make_sandbox_mock(output_json="[]", log_content=log_content)
    row = _FakeRunRow()

    def _factory() -> _RetentionSession:
        return _RetentionSession(row)

    fn = make_sandbox_agent_fn(_base_node_def(timeout_seconds=30), session_factory=_factory)

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())

    marker = _single_marker(row)
    assert result["output"]["agent_status"] is None
    assert "gho_secret789" not in marker["raw_output"]
    assert "github_pat_11AAABBBCC" not in marker["raw_output"]
    assert "ghp_zzzzzz" not in marker["raw_output"]
    assert "Bearer <redacted>" in marker["raw_output"]
    assert "token=<redacted>" in marker["raw_output"]
    assert "x-access-token:<redacted>" in marker["raw_output"]
    assert marker["pr_url"] == _PR_456


async def test_redaction_failure_never_blocks_retention():
    """FAR-188 QA round 2: if redaction itself raises (best-effort), the ORIGINAL
    content is retained and the marker still persists — retention must never be
    blocked by the scrub step, and pr_url extraction still ran first."""
    malformed = (
        '{"log": "https://x-access-token:ghp_abc123@github.com/org/repo.git", '
        '"pr_url": "https://github.com/org/repo/pull/42", '
    )
    fn, row, sandbox = _retention_env(output_json=malformed)

    _boom_pattern = MagicMock()
    _boom_pattern.sub.side_effect = re.error("boom")

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch(
            "modulo.core.pipeline_engine.node_runner._TOKENIZED_GIT_URL_PATTERN",
            new=_boom_pattern,
        ),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())

    marker = _single_marker(row)
    assert "ghp_abc123" in marker["raw_output"], "on redaction failure the original content is kept (fail open)"
    assert marker["pr_url"] == "https://github.com/org/repo/pull/42"
