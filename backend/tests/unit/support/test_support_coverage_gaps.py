"""Coverage-gap tests for the FAR-2305 support bundle internals.

``test_bundle.py`` pins the user-facing contract; these tests exercise the
degraded and defensive paths it does not reach: container mode (no launcher
data dir, no settings), unreadable DB/licence/alembic state, an oversized
bundle, CLI misuse, and each individual layer-5 gate reason.
"""

from __future__ import annotations

import asyncio
import json
import sys
import zipfile
from pathlib import Path
from typing import Any, ClassVar, Self

import pytest

import modulo.support as support_package
import modulo.support.bundle as bundle
from modulo.support.bundle import (
    BundleSources,
    BundleTooLargeError,
    RedactionGateError,
    build_bundle,
    main,
)
from modulo.support.redaction import (
    Redactor,
    _looks_like_unclassified_secret,
    _unquote,
    _url_password,
    collect_secret_values,
)

UNCLASSIFIED = "Zx9Qw8Er7Ty6Ui5Op4As3Df2Gh1J"
GITHUB_PAT = "ghp_abcdefghijklmnopqrstuvwxyz0123456789AB"


# ---------------------------------------------------------------------------
# package surface
# ---------------------------------------------------------------------------


def test_support_package_lazy_exports_and_unknown_attribute() -> None:
    assert support_package.BUNDLE_FORMAT_VERSION == bundle.BUNDLE_FORMAT_VERSION
    assert support_package.build_bundle is bundle.build_bundle
    with pytest.raises(AttributeError):
        _missing = support_package.no_such_export


# ---------------------------------------------------------------------------
# pure builders
# ---------------------------------------------------------------------------


def test_version_text_includes_build_environment() -> None:
    text = bundle.build_version_text({"GIT_SHA": "deadbeef", "BUILD_TIMESTAMP": "2026-11-02T00:00:00Z"})
    assert "build commit: deadbeef" in text
    assert "build timestamp: 2026-11-02T00:00:00Z" in text


def test_settings_text_without_a_settings_object() -> None:
    text = bundle.build_settings_text(None, {})
    assert "settings unavailable: no Settings object in this process" in text


def test_is_set_handles_collections() -> None:
    assert bundle._is_set([1]) is True
    assert bundle._is_set([]) is False


def test_deployment_text_without_data_dir() -> None:
    text = bundle.build_deployment_text(data_dir=None, environ={}, settings=None)
    assert "restart counts: unknown (no launcher data dir)" in text
    assert "install shape: unknown" in text


def test_degraded_json_none_and_persisted_record(tmp_path: Path) -> None:
    assert bundle.build_degraded_json(None) is None
    (tmp_path / "degraded.json").write_text(
        json.dumps({"reason": "postgres crash loop", "backtraces": ["trace"]}),
        encoding="utf-8",
    )
    payload = json.loads(bundle.build_degraded_json(tmp_path) or "{}")
    assert payload["degraded"] is True
    assert payload["reason"] == "postgres crash loop"
    assert payload["backtraces"] == ["trace"]


def test_resolve_licence_uses_the_settings_key_when_no_stored_licence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import modulo.core.license as licence_module

    class _Licence:
        tier = "pro"
        features: ClassVar[list[str]] = ["sso"]
        expires_at = "2030-01-01"
        licence_id = "lic_1"

    monkeypatch.setattr(licence_module, "get_license", lambda: None)
    monkeypatch.setattr(licence_module, "verify_license_key", lambda _key: _Licence())

    class _Settings:
        modulo_license_key = "KEY"

    payload = bundle.resolve_licence(_Settings())

    assert payload["tier"] == "pro"
    assert payload["licence_id"] == "lic_1"
    assert payload["days_until_expiry"] is not None
    assert payload["state"]


def test_resolve_licence_degrades_when_the_licence_read_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import modulo.core.license as licence_module

    def _boom() -> Any:
        raise RuntimeError("licence backend down")

    monkeypatch.setattr(licence_module, "get_license", _boom)

    payload = bundle.resolve_licence(None)
    assert payload["tier"] == "community"


def test_health_json_degrades_when_readiness_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import modulo.api.routes.health as health_module

    async def _boom() -> Any:
        raise RuntimeError("readiness exploded")

    monkeypatch.setattr(health_module, "evaluate_readiness", _boom)

    payload = json.loads(asyncio.run(bundle.build_health_json()))
    assert payload["status"] == "unknown"
    assert payload["error_code"] == "RuntimeError"
    assert not payload["checks"]


# ---------------------------------------------------------------------------
# DB / migration degradation
# ---------------------------------------------------------------------------


class _FakeBegin:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _NoOrgSession:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def begin(self) -> _FakeBegin:
        return _FakeBegin()

    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        class _Result:
            def scalar_one_or_none(self) -> None:
                return None

        return _Result()


def test_db_members_mark_all_missing_without_an_organisation() -> None:
    result = asyncio.run(bundle._read_db_members(lambda: _NoOrgSession()))

    assert {entry["member"] for entry in result.missing} == {"db.txt", "errors.json", "runs.json"}
    assert all(entry["reason"] == "no organisation row found" for entry in result.missing)


def test_read_one_records_a_reader_failure() -> None:
    async def _boom(_session: Any, _org_id: Any) -> None:
        raise RuntimeError("query failed")

    missing: list[dict[str, str]] = []
    value = asyncio.run(bundle._read_one(object(), "org", "db.txt", _boom, missing))

    assert value is None
    assert missing == [{"member": "db.txt", "reason": "read failed: RuntimeError"}]


def test_expected_migration_head_degrades_when_alembic_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import modulo.db.health_checks as health_checks

    def _boom() -> Any:
        raise RuntimeError("no alembic config")

    monkeypatch.setattr(health_checks, "resolve_alembic_ini", _boom)

    assert bundle._expected_migration_head() == "unknown"


def test_sanitize_message_rejects_a_non_string() -> None:
    assert not bundle._sanitize_message(123)


# ---------------------------------------------------------------------------
# log handling
# ---------------------------------------------------------------------------


def test_collapse_repeated_lines_truncates_to_the_max() -> None:
    collapsed, removed = bundle.collapse_repeated_lines("\n".join(["a", "b", "c", "d", "e"]), max_lines=2)

    lines = collapsed.splitlines()
    assert lines[0] == "... [truncated 3 lines]"
    assert lines[-2:] == ["d", "e"]
    assert removed == 3


def test_read_log_members_without_a_data_dir() -> None:
    assert not bundle._read_log_members(None, 1024)


# ---------------------------------------------------------------------------
# build_bundle degraded modes
# ---------------------------------------------------------------------------


def test_build_bundle_container_mode_keeps_going_without_settings_or_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import modulo.settings as settings_module

    def _boom() -> Any:
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(settings_module, "get_settings", _boom)
    out = tmp_path / "container.zip"

    result = build_bundle(
        out,
        sources=BundleSources(data_dir=None, doctor_output="doctor: ok", environ={}, enable_db=False),
    )

    assert out.is_file()
    reasons = {entry["member"]: entry["reason"] for entry in result.missing}
    assert reasons["degraded.json"].startswith("native launcher only")
    assert reasons["logs/*.log.tail"].startswith("no launcher data dir")
    assert "settings unavailable" in _member_text(out, "settings.txt")


def test_build_bundle_raises_when_even_shrunk_logs_are_too_large(
    tmp_path: Path,
) -> None:
    (tmp_path / "launcher.log").write_text("x" * 100, encoding="utf-8")

    with pytest.raises(BundleTooLargeError):
        build_bundle(
            tmp_path / "too-large.zip",
            sources=BundleSources(
                data_dir=tmp_path,
                doctor_output="doctor: ok",
                environ={},
                enable_db=False,
                max_bundle_bytes=1,
            ),
        )


def _member_text(path: Path, member: str) -> str:
    with zipfile.ZipFile(path) as archive:
        return archive.read(member).decode("utf-8")


# ---------------------------------------------------------------------------
# assembly helpers
# ---------------------------------------------------------------------------


def test_session_factory_returns_none_when_the_db_module_cannot_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "modulo.db.session", None)

    assert bundle._session_factory(BundleSources(enable_db=True, session_factory=None)) is None


def test_assert_allowlist_rejects_a_member_outside_the_fixed_set() -> None:
    with pytest.raises(RedactionGateError) as excinfo:
        bundle._assert_allowlist({"secrets.json": "{}"})

    assert excinfo.value.member == "secrets.json"


def test_render_meta_stops_after_the_bounded_number_of_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def _never_converges(**_kwargs: Any) -> str:
        calls["n"] += 1
        return "r" * calls["n"]

    monkeypatch.setattr(bundle, "_render_report", _never_converges)

    bundle._render_meta(ref="MSB-1-x", sizes={}, redaction_counts={}, missing=[], truncated=[])

    assert calls["n"] == 8


# ---------------------------------------------------------------------------
# reader / inspect tolerance
# ---------------------------------------------------------------------------


def test_inspect_bundle_tolerates_corrupt_and_missing_report(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(corrupt, "w") as archive:
        archive.writestr("report.json", "{not json")

    corrupt_result = bundle.inspect_bundle(corrupt)
    assert not corrupt_result.redaction_counts
    assert not corrupt_result.ref

    without_report = tmp_path / "without-report.zip"
    with zipfile.ZipFile(without_report, "w") as archive:
        archive.writestr("version.txt", "clean")

    missing_result = bundle.inspect_bundle(without_report)
    assert not missing_result.ref
    assert missing_result.members == (("version.txt", len(b"clean")),)


def test_bundle_ref_tokens_tolerates_missing_and_corrupt_report() -> None:
    assert not bundle._bundle_ref_tokens({})
    assert not bundle._bundle_ref_tokens({"report.json": b"{bad"})


def test_show_member_reports_read_missing_and_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    corrupt = tmp_path / "corrupt.zip"
    corrupt.write_bytes(b"not a zip")
    assert bundle._show_member(corrupt, "version.txt") == 1

    clean = tmp_path / "clean.zip"
    with zipfile.ZipFile(clean, "w") as archive:
        archive.writestr("version.txt", "clean")
    assert bundle._show_member(clean, "no-such-member") == 1

    secret = tmp_path / "secret.zip"
    with zipfile.ZipFile(secret, "w") as archive:
        archive.writestr("version.txt", UNCLASSIFIED)
    assert bundle._show_member(secret, "version.txt") == 2

    errors = capsys.readouterr().err
    assert "could not read bundle" in errors
    assert "has no member" in errors
    assert "refused" in errors


# ---------------------------------------------------------------------------
# CLI argument / write failures
# ---------------------------------------------------------------------------


def test_main_rejects_redact_stdin_combined_with_another_mode(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--redact-stdin", "--out", "bundle.zip"]) == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_main_requires_out_unless_redact_stdin(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "--out is required" in capsys.readouterr().err


def test_main_rejects_list_and_show_together(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--out", str(tmp_path / "bundle.zip"), "--list", "--show", "report.json"]) == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_main_reports_a_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(bundle, "build_bundle_async", _boom)

    assert main(["--out", str(tmp_path / "bundle.zip")]) == 1
    assert "could not write bundle" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# redaction internals
# ---------------------------------------------------------------------------


def test_collect_secret_values_includes_extra_values() -> None:
    seeds = collect_secret_values(data_dir=None, settings=None, extra={"token": UNCLASSIFIED})
    assert UNCLASSIFIED in seeds


def test_url_password_edge_cases() -> None:
    assert _url_password("not-a-url") is None
    assert _url_password("http://[::1") is None
    assert _url_password("postgresql://user@host/db") is None
    assert _url_password("postgresql://user:pw@host/db") == "pw"


def test_unquote_strips_a_matching_pair() -> None:
    assert _unquote('"quoted"') == "quoted"
    assert _unquote("bare") == "bare"


def test_gate_reports_a_seeded_secret_still_present() -> None:
    secret = "a" * 24
    with pytest.raises(RedactionGateError) as excinfo:
        Redactor({secret: "<redacted>"}).gate("member", secret)

    assert "seeded secret value still present" in "; ".join(excinfo.value.reasons)


def test_gate_reports_a_shared_pattern_match() -> None:
    with pytest.raises(RedactionGateError) as excinfo:
        Redactor({}).gate("member", f"token {GITHUB_PAT}")

    assert "shared secret-value pattern still matches" in "; ".join(excinfo.value.reasons)


def test_gate_reports_an_unredacted_sensitive_key() -> None:
    with pytest.raises(RedactionGateError) as excinfo:
        Redactor({}).gate("member", "DB_PASSWORD=plainvalue")

    assert "has an unredacted value" in "; ".join(excinfo.value.reasons)


def test_unclassified_secret_ignores_single_class_tokens() -> None:
    assert _looks_like_unclassified_secret("ABCDEF1234567890ABCDEF12") is False
    assert _looks_like_unclassified_secret("AbCdEfGhIjKlMnOpQrStUvWx") is False
    assert _looks_like_unclassified_secret("abcdef0123456789abcdef01") is False


def test_redact_stdin_preserves_input_without_a_trailing_newline(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import io

    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"clean line"), encoding="utf-8"))

    assert main(["--redact-stdin"]) == 0
    assert capsys.readouterr().out == "clean line"


def test_seed_values_skips_empty_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import modulo.launcher.secrets_file as secrets_file
    from modulo.support.redaction import seed_values_from_data_dir

    (tmp_path / "secrets.json").write_text("{}", encoding="utf-8")

    class _Empty:
        postgres_password = ""
        redis_password = ""
        state_hmac_key_hex = ""

    monkeypatch.setattr(secrets_file, "_parse", lambda _data: _Empty())

    assert not seed_values_from_data_dir(tmp_path)


def test_structured_layer_skips_already_redacted_values() -> None:
    result = Redactor({}).redact("DB_PASSWORD=<redacted>")

    assert result.counts["layer3_structural"] == 0
    assert result.text == "DB_PASSWORD=<redacted>"


def test_unclassified_secret_ignores_lowercase_and_uppercase_only_tokens() -> None:
    assert _looks_like_unclassified_secret("abcdefghijklmnopqrstuvwx") is False
    assert _looks_like_unclassified_secret("ABCDEFGHIJKLMNOPQRSTUVWX12") is False
