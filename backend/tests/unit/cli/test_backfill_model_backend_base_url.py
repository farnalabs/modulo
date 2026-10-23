"""Unit tests for the FAR-2067 legacy base_url backfill CLI.

These run with NO database: a fake session/engine and an ``AsyncMock``-style
secrets backend exercise the plan/apply/org-loop and key-recovery paths, because
CI's changed-lines coverage gate reads ``pytest tests/unit`` only — a path
covered exclusively by the real-Postgres integration file counts as uncovered.
``tests/integration/test_backfill_model_backend_base_url.py`` still proves the
destructive contract end to end (dry run writes nothing, apply, team-scoped
rows, cross-org isolation).

These tests also pin the classification rules (which must be the production
rules, not a copy), the report shape (host only), and the command/exit-code
contract.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from typing import Any, Self

import pytest
from click.testing import CliRunner
from cryptography.fernet import Fernet
from fastapi import HTTPException

import modulo.cli.backfill_model_backend_base_url as cli_mod
import modulo.cli.main as cli_main
import modulo.core.audit_logger as audit_logger
import modulo.core.model_backend_endpoints as shared_endpoints
import modulo.core.secrets_backend as secrets_backend_module
from modulo.api.routes import model_backends as route_module
from modulo.cli.backfill_model_backend_base_url import (
    AUDIT_ACTOR_SOURCE,
    AUDIT_EVENT_TYPE,
    BackfillResult,
    BackfillRow,
    _apply_row,
    _audit_applied_row,
    _plan_row,
    _process_org,
    _read_secret_payload,
    _render,
    _resolve_api_key,
    _stored_secret_payload,
    classify_legacy_endpoint,
    run_backfill,
)


@pytest.mark.parametrize(
    ("provider", "raw", "expected"),
    [
        ("openai", "https://gateway.internal.example/v1", "migrate"),
        ("ollama", "https://llm.internal.example:8443/v1", "migrate"),
        ("openai", "http://gateway.internal.example/v1", "strip-insecure"),
        ("openai", "http://10.0.0.5:8000/v1", "strip-insecure"),
        ("openai", "ftp://gateway.internal.example/v1", "reject-invalid"),
        ("openai", "https://user:pass@gateway.internal.example/v1", "reject-invalid"),
        ("openai", "https://gateway.internal.example/v1?token=leak", "reject-invalid"),
        ("openai", "https://169.254.169.254/latest/meta-data", "reject-invalid"),
        ("openai", "http://metadata.google.internal/v1", "reject-invalid"),
        ("openai", "http://2852039166/v1", "reject-invalid"),
        ("azure_openai", "https://gateway.internal.example/v1", "drop-incapable"),
        ("bedrock", "https://gateway.internal.example/v1", "drop-incapable"),
        ("vertexai", "https://gateway.internal.example/v1", "drop-incapable"),
        ("watsonx", "https://gateway.internal.example/v1", "drop-incapable"),
        ("custom", "https://gateway.internal.example/v1", "drop-incapable"),
        ("openai", "   ", "skip"),
        ("openai", "", "skip"),
        ("openai", None, "skip"),
        ("openai", 42, "skip"),
    ],
    ids=[
        "valid-https-openai",
        "valid-https-ollama",
        "plain-http-hostname",
        "plain-http-literal",
        "non-http-scheme",
        "userinfo",
        "query",
        "ipv4-metadata-literal",
        "metadata-hostname",
        "non-canonical-ipv4",
        "incapable-azure",
        "incapable-bedrock",
        "incapable-vertexai",
        "incapable-watsonx",
        "incapable-custom",
        "blank-value",
        "empty-value",
        "json-null",
        "non-string-value",
    ],
)
def test_classification_matches_the_production_rules(provider: str, raw: object, expected: str) -> None:
    assert classify_legacy_endpoint(provider, raw).outcome == expected


def test_classification_reports_only_the_host() -> None:
    classification = classify_legacy_endpoint(
        "openai",
        "https://gateway.internal.example/tenant/secret-path/v1",
    )
    assert classification.outcome == "migrate"
    assert classification.host == "gateway.internal.example"
    assert "secret-path" not in (classification.host or "")


def test_classification_carries_the_usable_value_for_the_credential() -> None:
    classification = classify_legacy_endpoint("openai", "https://gateway.internal.example/v1")
    assert classification.value == "https://gateway.internal.example/v1"


@pytest.mark.parametrize(
    "raw",
    [
        "  https://gateway.internal.example/v1  ",
        "https://gateway.internal.example/v1\n",
    ],
    ids=["padded", "trailing-newline"],
)
def test_classification_stores_the_validator_normalised_value(raw: str) -> None:
    """The CLI must store exactly what the REST write path would store.

    Storing the raw (padded) input writes a value the API never writes, and the
    drift then surfaces through ``stored_base_url`` → ``_effective_base_url`` →
    the API response.
    """
    expected = shared_endpoints.validate_base_url(raw)
    classification = classify_legacy_endpoint("openai", raw)
    assert classification.outcome == "migrate"
    assert classification.value == expected
    assert classification.value == "https://gateway.internal.example/v1"
    assert classification.host == "gateway.internal.example"


def test_classification_delegates_to_the_shared_validator(monkeypatch: pytest.MonkeyPatch) -> None:
    """A drifting copy would ignore this patched shared implementation."""
    seen: list[str] = []

    def _sentinel(value: str, *, allow_insecure_http: bool = False) -> str:
        seen.append(value)
        return value

    monkeypatch.setattr(shared_endpoints, "validate_base_url", _sentinel)
    classification = classify_legacy_endpoint("openai", "https://gateway.internal.example/v1")
    assert classification.outcome == "migrate"
    assert seen == ["https://gateway.internal.example/v1"]


_ROUTE_PARITY_INPUTS = [
    "",
    "   ",
    "https://gateway.internal.example/v1",
    "http://gateway.internal.example/v1",
    "ftp://gateway.internal.example/v1",
    "https://user:pass@gateway.internal.example/v1",
    "https://gateway.internal.example/v1?token=leak",
    "https://gateway.internal.example/v1#frag",
    "https://169.254.169.254/latest/meta-data",
    "http://metadata.google.internal/v1",
    "http://2852039166/v1",
    "https://example.com/" + "a" * 2048,
]


def _shared_error_code(value: str) -> str | None:
    try:
        shared_endpoints.validate_base_url(value)
    except shared_endpoints.BaseUrlValidationError as exc:
        return exc.code
    return None


def _route_error_code(value: str) -> str | None:
    try:
        route_module._validate_base_url(value)
    except HTTPException as exc:
        detail = exc.detail
        code = detail[0].get("code") if isinstance(detail, list) and detail else None
        return code if isinstance(code, str) else None
    return None


@pytest.mark.parametrize("value", _ROUTE_PARITY_INPUTS, ids=[f"input-{i}" for i in range(len(_ROUTE_PARITY_INPUTS))])
def test_route_and_shared_validator_agree_on_the_same_code(value: str) -> None:
    """Behavioural parity: the route's 422 ``code`` iff the shared rule raises.

    Pins WHAT the two paths do, not where the wrapper lives — a shared-rule
    change that only one path follows fails here.
    """
    assert _route_error_code(value) == _shared_error_code(value)


def test_cli_strips_http_where_the_route_accepts_an_explicit_opt_in() -> None:
    """The one deliberate difference: the CLI never auto-blesses plain http.

    The REST route accepts ``http://`` with an explicit, audited
    ``allow_insecure_http`` opt-in; the backfill has no operator present to
    grant that opt-in, so it strips instead.
    """
    raw = "http://gateway.internal.example/v1"
    assert route_module._validate_base_url(raw, allow_insecure_http=True) == raw
    assert classify_legacy_endpoint("openai", raw).outcome == "strip-insecure"


def test_classify_rejects_a_value_the_shared_validator_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    def _reject(_value: str, *, allow_insecure_http: bool = False) -> str:
        raise shared_endpoints.BaseUrlValidationError("blocked by the shared rule")

    monkeypatch.setattr(shared_endpoints, "validate_base_url", _reject)
    classification = classify_legacy_endpoint("openai", "https://gateway.internal.example/v1")
    assert classification.outcome == "reject-invalid"
    assert classification.detail == "blocked by the shared rule"


def _row(outcome: str = "migrate", host: str | None = "gateway.internal.example") -> BackfillRow:
    return BackfillRow(
        backend_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        organisation_slug="acme",
        provider="openai",
        outcome=outcome,  # type: ignore[arg-type]
        host=host,
    )


def test_render_reports_ids_provider_org_and_host_only(capsys: pytest.CaptureFixture[str]) -> None:
    result = BackfillResult(rows=[_row()], applied=False)
    cli_mod._render(result)
    output = capsys.readouterr().out
    row = result.rows[0]
    assert str(row.backend_id) in output
    assert str(row.organisation_slug) in output
    assert row.provider in output
    assert "host=gateway.internal.example" in output
    assert "https://" not in output
    assert "Nothing was written (dry run)" in output


def test_render_says_nothing_to_do_on_a_second_run(capsys: pytest.CaptureFixture[str]) -> None:
    cli_mod._render(BackfillResult(rows=[], applied=True))
    output = capsys.readouterr().out
    assert "No legacy default_params.base_url values found — nothing to do." in output


def test_requires_attention_only_for_stripped_or_rejected_rows() -> None:
    assert not BackfillResult(rows=[_row("migrate"), _row("skip")]).requires_attention
    assert BackfillResult(rows=[_row("strip-insecure")]).requires_attention
    assert BackfillResult(rows=[_row("reject-invalid")]).requires_attention
    assert BackfillResult(rows=[_row("drop-incapable")]).requires_attention
    assert BackfillResult(rows=[], failures=["org boom"]).requires_attention


def _install_fake_run(monkeypatch: pytest.MonkeyPatch, result: BackfillResult) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    async def _fake_run(**kwargs: object) -> BackfillResult:
        calls.append(kwargs)
        return result

    monkeypatch.setattr(cli_mod, "run_backfill", _fake_run)
    return calls


def test_command_is_registered_on_the_modulo_group() -> None:
    result = CliRunner().invoke(cli_main.cli, ["backfill-model-backend-base-url", "--help"])
    assert result.exit_code == 0
    assert "backfill-model-backend-base-url" in result.output


def test_command_is_a_dry_run_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_run(monkeypatch, BackfillResult(rows=[_row()]))
    result = CliRunner().invoke(cli_mod.backfill_model_backend_base_url, [])
    assert result.exit_code == 0
    assert calls == [{"apply": False, "organisation_id": None}]


def test_command_passes_apply_and_org_id(monkeypatch: pytest.MonkeyPatch) -> None:
    org_id = uuid.uuid4()
    calls = _install_fake_run(monkeypatch, BackfillResult(rows=[_row()], applied=True))
    result = CliRunner().invoke(
        cli_mod.backfill_model_backend_base_url,
        ["--apply", "--org-id", str(org_id)],
    )
    assert result.exit_code == 0
    assert calls == [{"apply": True, "organisation_id": org_id}]


def test_command_exits_non_zero_when_anything_was_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_run(monkeypatch, BackfillResult(rows=[_row("strip-insecure")], applied=True))
    result = CliRunner().invoke(cli_mod.backfill_model_backend_base_url, ["--apply"])
    assert result.exit_code == 1
    assert "strip-insecure" in result.output


def test_command_rejects_a_malformed_org_id(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_run(monkeypatch, BackfillResult(rows=[_row()]))
    result = CliRunner().invoke(cli_mod.backfill_model_backend_base_url, ["--org-id", "not-a-uuid"])
    assert result.exit_code == 2
    assert not calls


def test_render_prints_row_detail_and_failures(capsys: pytest.CaptureFixture[str]) -> None:
    row = BackfillRow(
        backend_id=uuid.uuid4(),
        organisation_id=_ORG_ID,
        organisation_slug="acme",
        provider="openai",
        outcome="migrate",
        host="gateway.internal.example",
        detail="credential endpoint already set; preserved",
    )
    _render(BackfillResult(rows=[row], failures=["org acme (x): RuntimeError: boom"], applied=True))
    captured = capsys.readouterr()
    assert "preserved" in captured.out
    assert "ERROR org acme (x): RuntimeError: boom" in captured.err


def test_command_wraps_an_unexpected_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(**_kwargs: object) -> BackfillResult:
        raise RuntimeError("db down")

    monkeypatch.setattr(cli_mod, "run_backfill", _boom)
    result = CliRunner().invoke(cli_mod.backfill_model_backend_base_url, [])
    assert result.exit_code == 1
    assert "backfill failed: RuntimeError: db down" in result.output


def test_command_passes_a_click_exception_through(monkeypatch: pytest.MonkeyPatch) -> None:
    import click

    async def _not_found(**_kwargs: object) -> BackfillResult:
        raise click.ClickException("organisation not found")

    monkeypatch.setattr(cli_mod, "run_backfill", _not_found)
    result = CliRunner().invoke(cli_mod.backfill_model_backend_base_url, ["--org-id", str(uuid.uuid4())])
    assert result.exit_code == 1
    assert "organisation not found" in result.output


# ── Plan / apply / org-loop / key recovery (no database) ─────────────────────
#
# CI's changed-lines coverage gate reads `pytest tests/unit`, so the destructive
# path must be exercised here too — the real-Postgres integration file proves the
# contract end to end but contributes no unit coverage.

_ORG_ID = uuid.uuid4()
_FERNET_KEY = Fernet.generate_key().decode()
_ORG = SimpleNamespace(id=_ORG_ID, slug="acme")


def _settings() -> SimpleNamespace:
    return SimpleNamespace(fernet_key=_FERNET_KEY)


class _FakeDialect:
    name = "sqlite"


class _FakeBind:
    dialect = _FakeDialect()


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class _FakeSession:
    """Minimal AsyncSession stand-in: transaction state + a canned result set."""

    def __init__(self, rows: list[Any] | None = None, *, execute_error: Exception | None = None) -> None:
        self.info: dict[str, Any] = {}
        self.statements: list[Any] = []
        self._rows = list(rows or [])
        self._execute_error = execute_error

    def in_transaction(self) -> bool:
        return True

    def get_bind(self) -> _FakeBind:
        return _FakeBind()

    async def execute(self, statement: Any) -> _FakeResult:
        if self._execute_error is not None:
            raise self._execute_error
        self.statements.append(statement)
        return _FakeResult(self._rows)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc_info: object) -> bool:
        return False

    def begin(self) -> _FakeSession:
        return self


class _FakeFactory:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def __call__(self) -> _FakeSession:
        return self._session


class _FakeBackend:
    def __init__(
        self,
        *,
        provider: str = "openai",
        default_params: dict[str, Any] | None = None,
        ciphertext: bytes | None = None,
    ) -> None:
        self.id = uuid.uuid4()
        self.provider = provider
        self.default_params = default_params if default_params is not None else {}
        self.credentials_ciphertext = ciphertext


class _FakeSecrets:
    def __init__(self, values: dict[str, str] | None = None, *, error: Exception | None = None) -> None:
        self._values = values or {}
        self._error = error
        self.writes: list[tuple[str, str]] = []

    async def get_secret(self, key: str) -> str:
        if self._error is not None:
            raise self._error
        if key not in self._values:
            raise KeyError(key)
        return self._values[key]

    async def set_secret(self, key: str, value: str) -> None:
        self.writes.append((key, value))


@pytest.mark.parametrize("raw", [None, "", 42, "{not-json", '["not", "an", "object"]'])
def test_stored_secret_payload_treats_anything_but_an_object_as_empty(raw: object) -> None:
    assert not _stored_secret_payload(raw)  # type: ignore[arg-type]


def test_stored_secret_payload_parses_an_object() -> None:
    assert _stored_secret_payload('{"api_key": "sk-x"}') == {"api_key": "sk-x"}


@pytest.mark.parametrize("error", [KeyError("missing"), ValueError("corrupt")])
async def test_read_secret_payload_treats_a_missing_or_corrupt_row_as_empty(error: Exception) -> None:
    assert not (await _read_secret_payload(_FakeSecrets(error=error), uuid.uuid4()))


async def test_read_secret_payload_parses_a_stored_object() -> None:
    backend_id = uuid.uuid4()
    secrets = _FakeSecrets({str(backend_id): '{"api_key": "sk-x"}'})
    assert await _read_secret_payload(secrets, backend_id) == {"api_key": "sk-x"}


async def test_resolve_api_key_prefers_the_stored_payload_key() -> None:
    backend = _FakeBackend(ciphertext=b"gAAAA-unused")
    resolved = await _resolve_api_key(_FakeSession(), backend, {"api_key": "sk-stored"}, _settings(), _ORG_ID)
    assert resolved == "sk-stored"


async def test_resolve_api_key_requires_a_ciphertext_when_the_payload_has_none() -> None:
    with pytest.raises(ValueError, match="no stored API key"):
        await _resolve_api_key(_FakeSession(), _FakeBackend(ciphertext=None), {}, _settings(), _ORG_ID)


async def test_resolve_api_key_recovers_a_ciphertext_only_credential() -> None:
    fernet = Fernet(_FERNET_KEY.encode())
    backend = _FakeBackend(ciphertext=fernet.encrypt(b"sk-recovered"))
    payload: dict[str, Any] = {}
    assert await _resolve_api_key(_FakeSession(), backend, payload, _settings(), _ORG_ID) == "sk-recovered"
    assert payload["api_key"] == "sk-recovered"


async def test_resolve_api_key_rejects_a_ciphertext_that_decrypts_to_empty() -> None:
    fernet = Fernet(_FERNET_KEY.encode())
    backend = _FakeBackend(ciphertext=fernet.encrypt(b""))
    with pytest.raises(ValueError, match="recovered API key is empty"):
        await _resolve_api_key(_FakeSession(), backend, {}, _settings(), _ORG_ID)


async def test_plan_row_leaves_a_non_migrating_row_untouched() -> None:
    backend = _FakeBackend(provider="azure_openai", default_params={"base_url": "https://gw.example/v1"})
    planned = await _plan_row(_FakeSession(), backend, _ORG_ID, "acme", _settings(), _FakeSecrets())
    assert planned.report.outcome == "drop-incapable"
    assert planned.write_secret is False
    assert planned.strip_legacy is True
    assert not planned.payload


async def test_plan_row_preserves_an_already_stored_credential_endpoint() -> None:
    backend = _FakeBackend(default_params={"base_url": "https://legacy.example/v1"})
    secrets = _FakeSecrets({str(backend.id): json.dumps({"api_key": "sk-x", "base_url": "https://current.example/v1"})})
    planned = await _plan_row(_FakeSession(), backend, _ORG_ID, "acme", _settings(), secrets)
    assert planned.report.outcome == "migrate"
    assert "preserved" in (planned.report.detail or "")
    assert planned.write_secret is False
    assert planned.strip_legacy is True
    assert planned.payload["base_url"] == "https://current.example/v1"


async def test_plan_row_leaves_the_row_untouched_when_the_credential_is_unrecoverable() -> None:
    # A structurally-complete Fernet token no configured key can decrypt
    # (a bare non-token byte string would be accepted as legacy plaintext by
    # the shared decoder, so it cannot exercise the failure arm).
    backend = _FakeBackend(
        default_params={"base_url": "https://legacy.example/v1", "temperature": 0.2},
        ciphertext=b"gAAAAA-not-a-valid-token",
    )
    secrets = _FakeSecrets()
    planned = await _plan_row(_FakeSession(), backend, _ORG_ID, "acme", _settings(), secrets)
    assert planned.report.outcome == "reject-invalid"
    assert planned.report.detail == "credential unavailable; legacy endpoint left in place"
    assert planned.write_secret is False
    assert planned.strip_legacy is False
    assert not planned.payload
    assert backend.default_params == {"base_url": "https://legacy.example/v1", "temperature": 0.2}


async def test_plan_row_migrates_a_usable_endpoint_into_the_credential() -> None:
    backend = _FakeBackend(default_params={"base_url": "  https://legacy.example/v1  "})
    secrets = _FakeSecrets({str(backend.id): json.dumps({"api_key": "sk-x"})})
    planned = await _plan_row(_FakeSession(), backend, _ORG_ID, "acme", _settings(), secrets)
    assert planned.report.outcome == "migrate"
    assert planned.report.host == "legacy.example"
    assert planned.write_secret is True
    assert planned.strip_legacy is True
    assert planned.payload["base_url"] == "https://legacy.example/v1"


def _planned(report: BackfillRow, backend: _FakeBackend, **kwargs: object) -> cli_mod._PlannedRow:
    return cli_mod._PlannedRow(report=report, backend=backend, **kwargs)  # type: ignore[arg-type]


async def test_apply_row_writes_the_credential_and_strips_the_legacy_key() -> None:
    backend = _FakeBackend(default_params={"base_url": "https://legacy.example/v1", "temperature": 0.2})
    payload = {"api_key": "sk-x", "base_url": "https://legacy.example/v1"}
    planned = _planned(_row(), backend, payload=payload, write_secret=True, strip_legacy=True)
    secrets = _FakeSecrets()
    assert await _apply_row(secrets, planned) is True
    assert secrets.writes == [(str(backend.id), json.dumps(payload))]
    assert backend.default_params == {"temperature": 0.2}


async def test_apply_row_is_a_noop_when_nothing_changed() -> None:
    backend = _FakeBackend(default_params={"temperature": 0.2})
    planned = _planned(_row(), backend, strip_legacy=True)
    secrets = _FakeSecrets()
    assert await _apply_row(secrets, planned) is False
    assert not secrets.writes
    assert backend.default_params == {"temperature": 0.2}


async def test_apply_row_does_nothing_when_no_write_is_planned() -> None:
    """The unrecoverable-credential arm reaches apply with neither flag set."""
    backend = _FakeBackend(default_params={"base_url": "https://legacy.example/v1"})
    planned = _planned(_row(), backend)
    secrets = _FakeSecrets()
    assert await _apply_row(secrets, planned) is False
    assert not secrets.writes
    assert backend.default_params == {"base_url": "https://legacy.example/v1"}


async def test_process_org_propagates_a_cancelled_audit_append(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _FakeBackend(default_params={"base_url": "https://a.example/v1"})
    secrets = _FakeSecrets({str(backend.id): json.dumps({"api_key": "sk-a"})})
    monkeypatch.setattr(secrets_backend_module, "create_secrets_backend", lambda **_kwargs: secrets)

    async def _cancelled(*_args: object, **_kwargs: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(audit_logger, "append_audit_event", _cancelled)
    with pytest.raises(asyncio.CancelledError):
        await _process_org(_FakeFactory(_FakeSession(rows=[backend])), _settings(), _ORG, apply=True)


async def test_audit_applied_row_carries_the_outcome_and_host_only(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[dict[str, Any]] = []

    async def _fake_append(_session: object, **kwargs: Any) -> None:
        events.append(kwargs)

    monkeypatch.setattr(audit_logger, "append_audit_event", _fake_append)
    report = _row("migrate", host="gateway.internal.example")
    await _audit_applied_row(_FakeSession(), _planned(report, _FakeBackend()))
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == AUDIT_EVENT_TYPE
    assert event["resource_type"] == "model_backend"
    assert event["resource_id"] == report.backend_id
    assert event["org_id"] == report.organisation_id
    payload = event["payload_json"]
    assert payload["outcome"] == "migrate"
    assert payload["host"] == "gateway.internal.example"
    assert payload["actor"] == "system"
    assert payload["actor_source"] == AUDIT_ACTOR_SOURCE
    assert "https://" not in json.dumps(payload)


async def test_process_org_plans_without_writing_on_a_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _FakeBackend(default_params={"base_url": "https://legacy.example/v1"})
    secrets = _FakeSecrets({str(backend.id): json.dumps({"api_key": "sk-x"})})
    monkeypatch.setattr(secrets_backend_module, "create_secrets_backend", lambda **_kwargs: secrets)
    session = _FakeSession(rows=[backend])

    rows, failures = await _process_org(_FakeFactory(session), _settings(), _ORG, apply=False)

    assert not failures
    assert [row.outcome for row in rows] == ["migrate"]
    assert not secrets.writes
    assert backend.default_params == {"base_url": "https://legacy.example/v1"}
    # Internal-execution context is set alongside the org GUC: without it a
    # `visibility='team'` row is invisible to this org-scoped session.
    assert session.info["execution_context"] is True
    assert session.info["org_id"] == _ORG_ID


async def test_process_org_applies_and_audits_every_changed_row(monkeypatch: pytest.MonkeyPatch) -> None:
    migratable = _FakeBackend(default_params={"base_url": "https://a.example/v1"})
    insecure = _FakeBackend(default_params={"base_url": "http://b.example/v1"})
    untouched = _FakeBackend(default_params={"temperature": 0.2})
    secrets = _FakeSecrets({str(migratable.id): json.dumps({"api_key": "sk-a"})})
    monkeypatch.setattr(secrets_backend_module, "create_secrets_backend", lambda **_kwargs: secrets)
    events: list[dict[str, Any]] = []

    async def _fake_append(_session: object, **kwargs: Any) -> None:
        events.append(kwargs)

    monkeypatch.setattr(audit_logger, "append_audit_event", _fake_append)
    session = _FakeSession(rows=[migratable, insecure, untouched])

    rows, failures = await _process_org(_FakeFactory(session), _settings(), _ORG, apply=True)

    assert not failures
    assert {row.outcome for row in rows} == {"migrate", "strip-insecure"}
    assert untouched.id not in {row.backend_id for row in rows}
    # One event per CHANGED row — never one for the row with no legacy key.
    assert len(events) == 2
    assert {event["payload_json"]["outcome"] for event in events} == {"migrate", "strip-insecure"}
    assert {event["payload_json"]["host"] for event in events} == {"a.example", "b.example"}
    assert all(event["payload_json"]["actor"] == "system" for event in events)
    assert all("https://" not in json.dumps(event["payload_json"]) for event in events)
    # Only the migratable row gains a credential; the strip-only row is never
    # narrowed and its legacy key is removed.
    assert [key for key, _value in secrets.writes] == [str(migratable.id)]
    assert not insecure.default_params


async def test_process_org_reports_an_audit_failure_without_losing_the_row(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _FakeBackend(default_params={"base_url": "https://a.example/v1"})
    secrets = _FakeSecrets({str(backend.id): json.dumps({"api_key": "sk-a"})})
    monkeypatch.setattr(secrets_backend_module, "create_secrets_backend", lambda **_kwargs: secrets)

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("audit chain down")

    monkeypatch.setattr(audit_logger, "append_audit_event", _boom)
    session = _FakeSession(rows=[backend])

    rows, failures = await _process_org(_FakeFactory(session), _settings(), _ORG, apply=True)

    assert [row.outcome for row in rows] == ["migrate"]
    assert len(failures) == 1
    assert "audit append failed" in failures[0]
    # The append is failure-isolated: the credential rewrite was not lost.
    assert len(secrets.writes) == 1


async def test_process_org_reports_a_failed_org(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secrets_backend_module, "create_secrets_backend", lambda **_kwargs: _FakeSecrets())
    session = _FakeSession(execute_error=RuntimeError("boom"))

    rows, failures = await _process_org(_FakeFactory(session), _settings(), _ORG, apply=False)

    assert not rows
    assert len(failures) == 1
    assert "org acme" in failures[0]
    assert "boom" in failures[0]


async def test_run_backfill_processes_every_organisation(monkeypatch: pytest.MonkeyPatch) -> None:
    orgs = [
        SimpleNamespace(id=uuid.uuid4(), slug="acme"),
        SimpleNamespace(id=uuid.uuid4(), slug="globex"),
    ]
    session = _FakeSession(rows=orgs)
    seen: list[tuple[str, bool]] = []

    async def _fake_process(
        _factory: object,
        _settings: object,
        organisation: Any,
        *,
        apply: bool,
    ) -> tuple[list[BackfillRow], list[str]]:
        seen.append((organisation.slug, apply))
        row = BackfillRow(
            backend_id=uuid.uuid4(),
            organisation_id=organisation.id,
            organisation_slug=organisation.slug,
            provider="openai",
            outcome="migrate",
            host="gw.example",
        )
        return [row], []

    monkeypatch.setattr(cli_mod, "_process_org", _fake_process)
    result = await run_backfill(apply=True, settings=_settings(), session_factory=_FakeFactory(session))

    assert seen == [("acme", True), ("globex", True)]
    assert result.considered == 2
    assert result.applied is True


async def test_run_backfill_raises_for_an_unknown_org(monkeypatch: pytest.MonkeyPatch) -> None:
    import click

    monkeypatch.setattr(cli_mod, "_process_org", _never_called)
    with pytest.raises(click.ClickException):
        await run_backfill(
            apply=False,
            organisation_id=uuid.uuid4(),
            settings=_settings(),
            session_factory=_FakeFactory(_FakeSession(rows=[])),
        )


async def test_run_backfill_resolves_the_default_session_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    import modulo.db.session as db_session

    org = SimpleNamespace(id=uuid.uuid4(), slug="acme")
    factory = _FakeFactory(_FakeSession(rows=[org]))
    monkeypatch.setattr(db_session, "AsyncSessionLocal", factory)
    seen: list[str] = []

    async def _fake_process(_factory: object, _settings: object, organisation: Any, *, apply: bool) -> Any:
        seen.append(organisation.slug)
        return [], []

    monkeypatch.setattr(cli_mod, "_process_org", _fake_process)
    result = await run_backfill(apply=False, settings=_settings())

    assert seen == ["acme"]
    assert result.applied is False


async def _never_called(*_args: object, **_kwargs: object) -> tuple[list[BackfillRow], list[str]]:
    raise AssertionError("_process_org must not run when the organisation does not exist")
