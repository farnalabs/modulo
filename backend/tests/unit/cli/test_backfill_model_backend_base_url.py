"""Unit tests for the FAR-2067 legacy base_url backfill CLI.

The destructive path (dry-run writes nothing, apply, cross-org isolation) is
covered against real Postgres in
``tests/integration/test_backfill_model_backend_base_url.py``. These tests pin
the classification rules (which must be the production rules, not a copy), the
report shape (host only), and the command/exit-code contract.
"""

from __future__ import annotations

import uuid

import pytest
from click.testing import CliRunner

import modulo.cli.backfill_model_backend_base_url as cli_mod
import modulo.cli.main as cli_main
import modulo.core.model_backend_endpoints as shared_endpoints
from modulo.api.routes import model_backends as route_module
from modulo.cli.backfill_model_backend_base_url import (
    BackfillResult,
    BackfillRow,
    classify_legacy_endpoint,
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


def test_route_validator_is_the_same_implementation() -> None:
    """The REST write path and the CLI share one validator, not two copies."""
    assert route_module._validate_base_url_rules is shared_endpoints.validate_base_url
    assert route_module._validate_base_url.__module__ == "modulo.api.routes.model_backends"


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
    assert calls == []
