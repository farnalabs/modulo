"""Unit tests for modulo.core.guardrails.loop_intercept (FAR-211 T3).

Covers the agent-loop interior tool-call interception bridge: config
validation, glob pattern matching, event serialisation caps, block/warn/redact/
pass action semantics (before + after directions), the per-call latency budget
(timeout -> fail-open with audit, never wedges the loop), guard-the-guard
(detection failure -> fail-open), the local callback server round-trip via the
sandbox-side bridge client, and summary-only audit payloads.
"""

import asyncio
import json
import time
import uuid

import pytest

from modulo.core.eval_engine import EvalDefinition, EvalEngine, EvalResult, EvalType
from modulo.core.guardrails import FieldRedactionMode, FieldRedactionPolicy
from modulo.core.guardrails import loop_intercept as li
from modulo.core.guardrails.loop_intercept import (
    DEFAULT_LOOP_INTERCEPT_PATTERNS,
    MAX_LOOP_INTERCEPT_ARGS_BYTES,
    LoopInterceptConfig,
    LoopInterceptConfigError,
    bridge_client_source,
    parse_loop_intercept_config,
    run_loop_interception,
    serialize_tool_event,
    tool_matches_patterns,
    validate_loop_intercept_config_errors,
)
from modulo.core.guardrails.sandbox_bridge import BridgeClient

_ORG_ID = uuid.uuid4()

_SECRET = "ghp_" + "a" * 24


def _guardrail(
    *,
    name: str = "gr",
    action: str = "block",
    field: str = "args.url",
    pattern: str = r"ghp_[A-Za-z0-9]{20,}",
    redaction: list | None = None,
) -> EvalDefinition:
    config: dict = {
        "action": action,
        "interception_point": "input",
        "type": "regex",
        "field": field,
        "pattern": pattern,
    }
    if redaction is not None:
        config["redaction"] = redaction
    return EvalDefinition(
        id=uuid.uuid4(),
        org_id=_ORG_ID,
        name=name,
        eval_type=EvalType.GUARDRAIL,
        config=config,
        failure_behaviour="warn",
    )


def _event(tool_name: str = "git push", args: dict | None = None, direction: str = "before", summary: str = "") -> dict:
    return {
        "tool_name": tool_name,
        "args": args or {"url": f"https://x-access-token:{_SECRET}@github.com/acme/repo.git"},
        "direction": direction,
        "result_summary": summary,
    }


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_config_defaults():
    cfg = LoopInterceptConfig()
    assert cfg.enabled is True
    assert cfg.latency_budget_ms == 250
    assert cfg.intercept_tool_results is True
    assert cfg.block_on_guardrail is True
    assert set(cfg.intercepted_tool_patterns) == set(DEFAULT_LOOP_INTERCEPT_PATTERNS)


def test_config_parses_valid_explicit():
    cfg = parse_loop_intercept_config(
        {
            "enabled": True,
            "latency_budget_ms": 500,
            "intercepted_tool_patterns": ["git push*"],
            "intercept_tool_results": False,
            "block_on_guardrail": True,
        }
    )
    assert cfg.latency_budget_ms == 500
    assert cfg.intercepted_tool_patterns == ["git push*"]
    assert cfg.intercept_tool_results is False


@pytest.mark.parametrize(
    "raw",
    [
        "not-a-dict",
        {"latency_budget_ms": 0},
        {"latency_budget_ms": "fast"},
        {"intercepted_tool_patterns": "git push"},
        {"enabled": []},
    ],
)
def test_config_malformed_raises(raw):
    with pytest.raises(LoopInterceptConfigError):
        parse_loop_intercept_config(raw)


def test_config_error_validation_helper():
    errors = validate_loop_intercept_config_errors({"latency_budget_ms": 0})
    assert errors
    assert any("latency_budget_ms" in e for e in errors)
    assert not validate_loop_intercept_config_errors({"latency_budget_ms": 300})


# ---------------------------------------------------------------------------
# Pattern matching + event serialisation
# ---------------------------------------------------------------------------


def test_tool_pattern_matching():
    assert tool_matches_patterns("git push origin main", DEFAULT_LOOP_INTERCEPT_PATTERNS)
    assert tool_matches_patterns("gh pr create", DEFAULT_LOOP_INTERCEPT_PATTERNS)
    assert tool_matches_patterns("curl -X POST https://api", DEFAULT_LOOP_INTERCEPT_PATTERNS)
    assert not tool_matches_patterns("git status", DEFAULT_LOOP_INTERCEPT_PATTERNS)
    assert not tool_matches_patterns("cat /home/user/output.json", DEFAULT_LOOP_INTERCEPT_PATTERNS)


def test_serialize_tool_event_round_trips_args():
    payload = serialize_tool_event("git push", {"url": "https://x", "nested": {"a": [1, 2]}}, "before")
    assert payload["tool"] == "git push"
    assert payload["args"] == {"url": "https://x", "nested": {"a": [1, 2]}}
    assert payload["direction"] == "before"
    assert not payload["result_summary"]


def test_serialize_tool_event_caps_oversized_args():
    big = {"blob": "x" * (MAX_LOOP_INTERCEPT_ARGS_BYTES + 10)}
    payload = serialize_tool_event("git push", big, "before")
    assert payload["args"] == {"_truncated": True, "_arg_count": 1}


def test_serialize_tool_event_stringifies_non_json_args():
    payload = serialize_tool_event("git push", {"bad": object()}, "before")
    assert isinstance(payload["args"]["bad"], str)


def test_serialize_tool_event_non_dict_args():
    payload = serialize_tool_event("git push", "not-a-dict", "before")
    assert not payload["args"]


# ---------------------------------------------------------------------------
# Action semantics
# ---------------------------------------------------------------------------


async def test_block_before_refuses_call():
    cfg = LoopInterceptConfig()
    outcome, audit = await run_loop_interception(EvalEngine(), [_guardrail(name="block-token")], _event(), config=cfg)
    assert outcome.action == "block"
    assert outcome.blocked is True
    assert outcome.guardrail_name == "block-token"
    assert audit
    assert audit[0].event_type == "guardrail.loop_blocked"


async def test_block_before_with_block_on_guardrail_false_records_not_refuses():
    cfg = LoopInterceptConfig(block_on_guardrail=False)
    outcome, audit = await run_loop_interception(EvalEngine(), [_guardrail(name="block-token")], _event(), config=cfg)
    assert outcome.action == "block"
    assert outcome.blocked is False
    assert audit
    assert audit[0].event_type == "guardrail.loop_blocked"


async def test_block_after_records_not_refuses():
    cfg = LoopInterceptConfig()
    event = _event(direction="after", summary=f"pushed with {_SECRET}")
    outcome, audit = await run_loop_interception(
        EvalEngine(), [_guardrail(name="block-token", field="args.url")], event, config=cfg
    )
    assert outcome.action == "block"
    assert outcome.blocked is False
    assert audit[0].event_type == "guardrail.loop_blocked"


async def test_warn_records_and_proceeds():
    cfg = LoopInterceptConfig()
    outcome, audit = await run_loop_interception(
        EvalEngine(), [_guardrail(name="warn-token", action="warn")], _event(), config=cfg
    )
    assert outcome.action == "warn"
    assert outcome.blocked is False
    assert audit[0].event_type == "guardrail.loop_warned"


async def test_redact_masks_args_before_execution():
    cfg = LoopInterceptConfig()
    policy = FieldRedactionPolicy(path="args.url", mode=FieldRedactionMode.TRANSFORM)
    gr = _guardrail(name="redact-url", action="redact", redaction=[policy.model_dump()])
    outcome, audit = await run_loop_interception(EvalEngine(), [gr], _event(), config=cfg)
    assert outcome.action == "redact"
    assert outcome.masked_args is not None
    assert outcome.masked_args["url"] != _SECRET
    assert _SECRET not in outcome.masked_args["url"]
    assert audit[0].event_type == "guardrail.loop_redacted"


async def test_block_mode_redaction_policy_blocks_before():
    cfg = LoopInterceptConfig()
    policy = FieldRedactionPolicy(path="args.url", mode=FieldRedactionMode.BLOCK)
    gr = _guardrail(name="redact-block", action="redact", redaction=[policy.model_dump()])
    outcome, audit = await run_loop_interception(EvalEngine(), [gr], _event(), config=cfg)
    assert outcome.action == "block"
    assert outcome.blocked is True
    assert audit[0].event_type == "guardrail.loop_blocked"


async def test_non_intercepted_tool_passes_without_evaluation():
    cfg = LoopInterceptConfig()
    event = _event(tool_name="git status")
    outcome, audit = await run_loop_interception(EvalEngine(), [_guardrail()], event, config=cfg)
    assert outcome.action == "pass"
    assert outcome.blocked is False
    assert audit == []


async def test_zero_guardrails_passes():
    cfg = LoopInterceptConfig()
    outcome, audit = await run_loop_interception(EvalEngine(), [], _event(), config=cfg)
    assert outcome.action == "pass"
    assert audit == []


async def test_disabled_config_passes():
    cfg = LoopInterceptConfig(enabled=False)
    outcome, _ = await run_loop_interception(EvalEngine(), [_guardrail()], _event(), config=cfg)
    assert outcome.action == "pass"


async def test_result_interception_skipped_when_disabled():
    cfg = LoopInterceptConfig(intercept_tool_results=False)
    event = _event(direction="after", summary=f"contains {_SECRET}")
    outcome, audit = await run_loop_interception(EvalEngine(), [_guardrail(field="result_summary")], event, config=cfg)
    assert outcome.action == "pass"
    assert audit == []


# ---------------------------------------------------------------------------
# Latency budget + guard-the-guard (fail-open, never wedges the loop)
# ---------------------------------------------------------------------------


def _slow_detection(_engine, _payload, _def):
    time.sleep(0.5)
    return EvalResult(run_id=uuid.uuid4(), node_id="n1", eval_id=_def.id, passed=False, score=0.0, detail="slow")


async def test_latency_budget_timeout_allows_call_and_audits(monkeypatch):
    monkeypatch.setattr(li, "_detect_one", _slow_detection)
    cfg = LoopInterceptConfig(latency_budget_ms=50)
    outcome, audit = await run_loop_interception(EvalEngine(), [_guardrail()], _event(), config=cfg)
    assert outcome.action == "pass"
    assert outcome.blocked is False
    assert outcome.bridge_failed is True
    assert "latency" in outcome.reason
    assert audit
    assert audit[0].event_type == "guardrail.loop_bridge_timeout"


def _exploding_detection(_engine, _payload, _def):
    raise RuntimeError("boom")


async def test_detection_mechanism_error_fails_open(monkeypatch):
    monkeypatch.setattr(li, "_detect_one", _exploding_detection)
    cfg = LoopInterceptConfig()
    outcome, audit = await run_loop_interception(EvalEngine(), [_guardrail()], _event(), config=cfg)
    assert outcome.action == "pass"
    assert outcome.bridge_failed is True
    assert outcome.reason
    assert audit
    assert audit[0].event_type == "guardrail.loop_bridge_timeout"


# ---------------------------------------------------------------------------
# Summary-only audit payloads (no raw args/payloads)
# ---------------------------------------------------------------------------


async def test_audit_payloads_never_carry_raw_values():
    cfg = LoopInterceptConfig()
    outcome, audit = await run_loop_interception(EvalEngine(), [_guardrail(name="block-token")], _event(), config=cfg)
    assert outcome.action == "block"
    for record in audit:
        serialized = json.dumps(record.payload)
        assert _SECRET not in serialized
        assert "x-access-token" not in serialized
        assert record.payload["tool"] == "git push"
        assert record.payload["guardrail"] == "block-token"
        assert record.payload["direction"] == "before"


async def test_redact_audit_payload_summary_only():
    cfg = LoopInterceptConfig()
    policy = FieldRedactionPolicy(path="args.url", mode=FieldRedactionMode.TRANSFORM)
    gr = _guardrail(name="redact-url", action="redact", redaction=[policy.model_dump()])
    _, audit = await run_loop_interception(EvalEngine(), [gr], _event(), config=cfg)
    serialized = json.dumps(audit[0].payload)
    assert _SECRET not in serialized


# ---------------------------------------------------------------------------
# Callback server round-trip (localhost) + bridge client fail-open
# ---------------------------------------------------------------------------


async def test_callback_server_block_round_trip():
    server = li.LoopInterceptCallbackServer(
        EvalEngine(),
        [_guardrail(name="block-token")],
        LoopInterceptConfig(),
    )
    port = await server.start()
    try:
        # The callback server evaluates events on the running event loop, so the
        # blocking HTTP call must run in a worker thread (never block the loop).
        def _call() -> tuple[bool, dict | None, str]:
            client = BridgeClient(f"http://127.0.0.1:{port}", timeout=5.0)
            return client.decide_before("git push", _event()["args"])

        allowed, masked, action = await asyncio.to_thread(_call)
        assert allowed is False
        assert masked is None
        assert action == "block"
    finally:
        await server.close()


async def test_callback_server_redact_round_trip():
    policy = FieldRedactionPolicy(path="args.url", mode=FieldRedactionMode.TRANSFORM)
    gr = _guardrail(name="redact-url", action="redact", redaction=[policy.model_dump()])
    server = li.LoopInterceptCallbackServer(
        EvalEngine(),
        [gr],
        LoopInterceptConfig(),
    )
    port = await server.start()
    try:

        def _call() -> tuple[bool, dict | None, str]:
            client = BridgeClient(f"http://127.0.0.1:{port}", timeout=5.0)
            return client.decide_before("git push", _event()["args"])

        allowed, masked, action = await asyncio.to_thread(_call)
        assert allowed is True
        assert masked is not None
        assert action == "redact"
        assert _SECRET not in masked["url"]
    finally:
        await server.close()


async def test_callback_server_invokes_audit_sink():
    records: list[li.LoopInterceptAuditRecord] = []

    async def _sink(batch):
        records.extend(batch)

    server = li.LoopInterceptCallbackServer(
        EvalEngine(),
        [_guardrail(name="block-token")],
        LoopInterceptConfig(),
        audit_sink=_sink,
    )
    port = await server.start()
    try:

        def _call() -> tuple[bool, dict | None, str]:
            client = BridgeClient(f"http://127.0.0.1:{port}", timeout=5.0)
            return client.decide_before("git push", _event()["args"])

        await asyncio.to_thread(_call)
        await asyncio.sleep(0.1)
        assert any(r.event_type == "guardrail.loop_blocked" for r in records)
    finally:
        await server.close()


def test_bridge_client_fails_open_on_unreachable_endpoint():
    client = BridgeClient("http://127.0.0.1:1", timeout=0.5)
    allowed, masked, _ = client.decide_before("git push", _event()["args"])
    assert allowed is True
    assert masked is None


def test_bridge_client_skips_non_intercepted_tool():
    client = BridgeClient()
    assert client.should_intercept("git push origin main")
    assert not client.should_intercept("git status")
    decision = client.notify("git status", {}, "before")
    assert decision["action"] == "pass"
    assert decision["reason"] == "not-intercepted"


def test_bridge_client_source_reads_sandbox_script():
    source = bridge_client_source()
    assert "class BridgeClient" in source
    assert "MODULO_BRIDGE_EVENT" in source


async def test_callback_server_returns_pass_for_unknown_path():
    server = li.LoopInterceptCallbackServer(
        EvalEngine(),
        [_guardrail()],
        LoopInterceptConfig(),
    )
    port = await server.start()
    try:

        def _call() -> dict:
            client = BridgeClient(f"http://127.0.0.1:{port}", timeout=5.0)
            return client.notify("git push", _event()["args"], "before")

        decision = await asyncio.to_thread(_call)
        assert isinstance(decision, dict)
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# Bridge CLI / --wrap mode (MODULO_BRIDGE_EVENT / MODULO_BRIDGE_BLOCKED)
# ---------------------------------------------------------------------------


class _FakeProc:
    """Fake subprocess whose stdout yields the given byte lines and records
    whether it was killed. Reproduces the Popen(stdout=PIPE) contract used by
    ``_wrap_command`` without requiring /bin/sh (Linux-only)."""

    def __init__(self, lines: list[bytes], *, wait_code: int = 0) -> None:
        self._lines = iter(lines)
        self._wait_code = wait_code
        self.killed = False
        self.closed = False

    @property
    def stdout(self):
        return self

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        return next(self._lines)

    def close(self) -> None:
        self.closed = True

    def kill(self) -> None:
        self.killed = True

    def wait(self) -> int:
        return self._wait_code


def test_wrap_command_block_kills_child_and_emits_marker(monkeypatch, capsys):
    """A 'before' block decision from the Modulo side KILLS the wrapped child
    and writes a MODULO_BRIDGE_BLOCKED marker + non-zero exit (3)."""
    import modulo.core.guardrails.sandbox_bridge as sb

    event_line = b'MODULO_BRIDGE_EVENT: {"tool_name": "git push", "args": {"url": "x"}, "direction": "before"}'
    proc = _FakeProc([event_line], wait_code=0)
    monkeypatch.setattr(sb.subprocess, "Popen", lambda *a, **k: proc)

    # The Modulo side returns block for any intercepted before-call.
    monkeypatch.setattr(
        sb.BridgeClient,
        "decide_before",
        lambda self, tool, args: (False, None, "block"),
    )

    client = sb.BridgeClient("http://127.0.0.1:9", timeout=0.5)
    code = sb._wrap_command(["git", "push"], client)

    assert code == 3
    assert proc.killed is True
    out = capsys.readouterr().out
    assert "MODULO_BRIDGE_BLOCKED:git push" in out


def test_wrap_command_allowed_event_passes_through(monkeypatch, capsys):
    """An allowed 'before' event (and any non-event stdout line) is echoed
    through unchanged; the child is NOT killed."""
    import modulo.core.guardrails.sandbox_bridge as sb

    event_line = b'MODULO_BRIDGE_EVENT: {"tool_name": "git push", "args": {"url": "x"}, "direction": "before"}'
    proc = _FakeProc([b"agent says hello", event_line], wait_code=0)
    monkeypatch.setattr(sb.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(
        sb.BridgeClient,
        "decide_before",
        lambda self, tool, args: (True, None, "pass"),
    )

    client = sb.BridgeClient("http://127.0.0.1:9", timeout=0.5)
    code = sb._wrap_command(["git", "push"], client)

    assert code == 0
    assert proc.killed is False
    out = capsys.readouterr().out
    assert "agent says hello" in out
    assert "MODULO_BRIDGE_BLOCKED" not in out


def test_wrap_command_bridge_failure_is_fail_open(monkeypatch, capsys):
    """A bridge failure (unreachable endpoint) during wrap NEVER blocks the
    command — the child runs to completion (best-effort fail-open)."""
    import modulo.core.guardrails.sandbox_bridge as sb

    event_line = b'MODULO_BRIDGE_EVENT: {"tool_name": "git push", "args": {"url": "x"}, "direction": "before"}'
    proc = _FakeProc([event_line], wait_code=0)
    monkeypatch.setattr(sb.subprocess, "Popen", lambda *a, **k: proc)

    # Unreachable endpoint -> BridgeClient.decide_before returns (True, None, ...)
    # fail-open (the real client already fails open; this proves the wrap path
    # does not kill the child when the bridge cannot be reached).
    client = sb.BridgeClient("http://127.0.0.1:1", timeout=0.2)
    code = sb._wrap_command(["git", "push"], client)

    assert code == 0
    assert proc.killed is False
    out = capsys.readouterr().out
    assert "MODULO_BRIDGE_BLOCKED" not in out


def test_wrap_command_redact_emits_redacted_marker(monkeypatch, capsys):
    """A redact decision emits a MODULO_BRIDGE_REDACTED line (masked args) and
    the child is allowed to proceed."""
    import modulo.core.guardrails.sandbox_bridge as sb

    event_line = (
        b'MODULO_BRIDGE_EVENT: {"tool_name": "git push", "args": {"url": '
        b'"https://x-access-token:ghp_aaaaaaaaaaaaaaaaaaaaaaaa@github.com/a/b.git"}, '
        b'"direction": "before"}'
    )
    proc = _FakeProc([event_line], wait_code=0)
    monkeypatch.setattr(sb.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(
        sb.BridgeClient,
        "decide_before",
        lambda self, tool, args: (True, {"url": "https://REDACTED"}, "redact"),
    )

    client = sb.BridgeClient("http://127.0.0.1:9", timeout=0.5)
    code = sb._wrap_command(["git", "push"], client)

    assert code == 0
    assert proc.killed is False
    out = capsys.readouterr().out
    assert "MODULO_BRIDGE_REDACTED:" in out
    assert "REDACTED" in out


def test_notify_cli_exit_code_3_on_block(monkeypatch):
    """The ``--notify`` CLI returns exit code 3 on a block decision, 0 on pass."""
    import modulo.core.guardrails.sandbox_bridge as sb

    monkeypatch.setattr(
        sb.BridgeClient,
        "notify",
        lambda self, tool, args, direction, result_summary="": {"action": "block", "blocked": True},
    )
    assert sb.main(["--notify", '{"tool_name": "git push", "args": {}, "direction": "before"}', "--endpoint", "x"]) == 3

    monkeypatch.setattr(
        sb.BridgeClient,
        "notify",
        lambda self, tool, args, direction, result_summary="": {"action": "pass", "blocked": False},
    )
    assert (
        sb.main(["--notify", '{"tool_name": "git status", "args": {}, "direction": "before"}', "--endpoint", "x"]) == 0
    )


def test_notify_cli_downgraded_block_exit_0(monkeypatch):
    """A ``block_on_guardrail: false`` downgrade (action=block, blocked=false)
    must NOT make the ``--notify`` CLI exit non-zero — the block is record-only."""
    import modulo.core.guardrails.sandbox_bridge as sb

    monkeypatch.setattr(
        sb.BridgeClient,
        "notify",
        lambda self, tool, args, direction, result_summary="": {"action": "block", "blocked": False},
    )
    assert sb.main(["--notify", '{"tool_name": "git push", "args": {}, "direction": "before"}', "--endpoint", "x"]) == 0


def test_decide_before_honours_blocked_flag(monkeypatch):
    """``decide_before`` must refuse ONLY on an actual refusal (action=block AND
    blocked=true). A downgraded block (action=block, blocked=false) — the
    ``block_on_guardrail: false`` case — must be allowed (record-only)."""
    import modulo.core.guardrails.sandbox_bridge as sb

    # blocked=true -> refuse.
    monkeypatch.setattr(
        sb.BridgeClient,
        "notify",
        lambda self, tool, args, direction, result_summary="": {"action": "block", "blocked": True},
    )
    client = sb.BridgeClient("http://127.0.0.1:9", timeout=0.5)
    allowed, _masked, action = client.decide_before("git push", {"url": "x"})
    assert allowed is False
    assert action == "block"

    # blocked=false (block_on_guardrail: false downgrade) -> allowed, record-only.
    monkeypatch.setattr(
        sb.BridgeClient,
        "notify",
        lambda self, tool, args, direction, result_summary="": {"action": "block", "blocked": False},
    )
    allowed, _masked, action = client.decide_before("git push", {"url": "x"})
    assert allowed is True
    assert action == "block"


def test_wrap_command_downgraded_block_does_not_kill(monkeypatch, capsys):
    """A ``block_on_guardrail: false`` downgrade (action=block, blocked=false)
    must NOT kill the wrapped child — the call proceeds (record-only)."""
    import modulo.core.guardrails.sandbox_bridge as sb

    event_line = b'MODULO_BRIDGE_EVENT: {"tool_name": "git push", "args": {"url": "x"}, "direction": "before"}'
    proc = _FakeProc([event_line], wait_code=0)
    monkeypatch.setattr(sb.subprocess, "Popen", lambda *a, **k: proc)
    # The Modulo side downgrades the block (blocked=false) for this before-call.
    monkeypatch.setattr(
        sb.BridgeClient,
        "notify",
        lambda self, tool, args, direction, result_summary="": {"action": "block", "blocked": False},
    )

    client = sb.BridgeClient("http://127.0.0.1:9", timeout=0.5)
    code = sb._wrap_command(["git", "push"], client)

    assert code == 0
    assert proc.killed is False
    out = capsys.readouterr().out
    assert "MODULO_BRIDGE_BLOCKED" not in out


def test_load_config_resolves_patterns(tmp_path, monkeypatch):
    """``_load_config`` resolves intercepted_tool_patterns from the config file
    and falls back to defaults when the field is absent/malformed."""
    import modulo.core.guardrails.sandbox_bridge as sb

    monkeypatch.chdir(tmp_path)

    assert sb._load_config(None) == {"patterns": sb.DEFAULT_PATTERNS}
    assert sb._load_config(str(tmp_path / "missing.json")) == {"patterns": sb.DEFAULT_PATTERNS}

    cfg_file = tmp_path / "bridge.json"
    cfg_file.write_text(json.dumps({"intercepted_tool_patterns": ["git push*"]}), encoding="utf-8")
    assert sb._load_config(str(cfg_file)) == {"patterns": ("git push*",)}

    bad_file = tmp_path / "bad.json"
    bad_file.write_text("not json", encoding="utf-8")
    assert sb._load_config(str(bad_file)) == {"patterns": sb.DEFAULT_PATTERNS}
