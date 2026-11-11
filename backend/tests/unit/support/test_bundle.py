"""Support bundle format v1 (FAR-2305) acceptance tests.

Covers the contract the ticket pins down:

* the planted-secret fixture (a) — every generated/known secret shape in every
  log is gone, ``secrets.json`` is absent, and every redaction layer that
  applied reports a substitution count;
* the residual gate (b) — a secret shape no layer covers WITHHOLDS only the
  offending item (FAR-2333), keeps the rest of the bundle, and records the
  withholding in MANIFEST.txt and report.json;
* the golden member set (c);
* ``--list`` / ``--show`` never print an unredacted value (d);
* unknown-member forward compatibility (e);
* crash-loop log collapse (f);
* ``modulo doctor --report`` still produces a valid v1 bundle (g).
"""

from __future__ import annotations

import io
import json
import re
import sys
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar, Self

import pytest

import farna.support.bundle as bundle_module
from farna.support.bundle import (
    BUNDLE_FORMAT_VERSION,
    GOLDEN_MEMBERS,
    BundleSources,
    build_bundle,
    generate_ref,
    inspect_bundle,
    is_allowed_member,
    main,
    read_bundle,
)
from farna.support.redaction import REDACTED

POSTGRES_PASSWORD = "PG_PLANT_zz19-weird-real-looking"
REDIS_PASSWORD = "RD_PLANT_k4-real-cookie-value"
HMAC_KEY_HEX = "a1b2" * 16
GITHUB_PAT = "ghp_abcdefghijklmnopqrstuvwxyz0123456789AB"
OPENAI_KEY = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
BEARER_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
SETTINGS_SECRET = "settings-secret-key-value-that-must-not-leak"
SETTINGS_USERS = "admin@example.com:hunter2-in-a-users-list"
UNCLASSIFIED_SECRET = "Xq7Lp2Rt9Vw4Bn6Mc8Kd3Fg5Hj1Sz0"

#: The per-run reference shape BOTH producers (this package and the Compose
#: collector) must emit.
REF_SHAPE = re.compile(r"^MSB-[0-9]+-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")

ALL_PLANTED = (
    POSTGRES_PASSWORD,
    REDIS_PASSWORD,
    HMAC_KEY_HEX,
    GITHUB_PAT,
    OPENAI_KEY,
    BEARER_TOKEN,
    SETTINGS_SECRET,
    SETTINGS_USERS,
    REDIS_PASSWORD,
)


class _FakeSettings:
    """Deterministic Settings seam: no real environment is read."""

    model_fields: ClassVar[dict[str, Any]] = {
        "modulo_db": None,
        "modulo_public_url": None,
        "saq_worker_concurrency": None,
        "secret_key": None,
        "modulo_users": None,
        "database_url": None,
    }
    modulo_db = "postgres"
    modulo_public_url = "https://modulo.example"
    saq_worker_concurrency = 4
    secret_key = SETTINGS_SECRET
    modulo_users = SETTINGS_USERS
    database_url = f"postgresql://modulo:{POSTGRES_PASSWORD}@db:5432/modulo"


def _seed_data_dir(tmp_path: Path, log_lines: str) -> None:
    (tmp_path / "secrets.json").write_text(
        json.dumps(
            {
                "postgres_password": POSTGRES_PASSWORD,
                "redis_password": REDIS_PASSWORD,
                "state_hmac_key": HMAC_KEY_HEX,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "launcher.log").write_text(log_lines, encoding="utf-8")
    (tmp_path / "logs" / "postgres.log").write_text(log_lines, encoding="utf-8")
    (tmp_path / "logs" / "redis.log").write_text(log_lines, encoding="utf-8")


def _planted_log() -> str:
    return "\n".join(
        [
            f"pg password failed: {POSTGRES_PASSWORD}",
            f"redis requirepass {REDIS_PASSWORD}",
            f"hmac key {HMAC_KEY_HEX}",
            f"github token {GITHUB_PAT}",
            f"openai key {OPENAI_KEY}",
            f"authorization: Bearer {BEARER_TOKEN}",
            f"dsn redis://default:{REDIS_PASSWORD}@127.0.0.1:6379/0",
            f"settings secret {SETTINGS_SECRET}",
            f"users list {SETTINGS_USERS}",
            "WEBHOOK_SECRET=not-a-seeded-value-yet-still-sensitive",
        ]
    )


def _member_blob(archive_path: Path) -> bytes:
    with zipfile.ZipFile(archive_path) as archive:
        return b"".join(archive.read(name) for name in archive.namelist())


def _build_full(tmp_path: Path) -> Path:
    out = tmp_path / "bundle.zip"
    build_bundle(
        out,
        sources=BundleSources(
            data_dir=tmp_path,
            doctor_output="modulo doctor: healthy",
            settings=_FakeSettings(),
            environ={},
            enable_db=False,
        ),
    )
    return out


# ---------------------------------------------------------------------------
# (a) planted-secret fixture
# ---------------------------------------------------------------------------


def test_planted_secrets_never_survive_any_member(tmp_path: Path) -> None:
    _seed_data_dir(tmp_path, _planted_log())
    out = _build_full(tmp_path)

    blob = _member_blob(out)
    for secret in ALL_PLANTED:
        assert secret.encode() not in blob, f"planted secret {secret[:12]!r} leaked"

    with zipfile.ZipFile(out) as archive:
        names = archive.namelist()
        report = json.loads(archive.read("report.json"))
    assert "secrets.json" not in names
    assert not any(name.endswith("secrets.json.tail") for name in names)
    # Each applied layer reports its substitutions.
    assert report["redaction"]["layer2_exact"] > 0
    assert report["redaction"]["layer3_structural"] > 0
    assert report["redaction"]["layer4_patterns"] > 0
    assert report["bundle_format_version"] == BUNDLE_FORMAT_VERSION


def test_settings_inventory_hides_unsafe_values(tmp_path: Path) -> None:
    _seed_data_dir(tmp_path, "clean log")
    out = _build_full(tmp_path)
    with zipfile.ZipFile(out) as archive:
        settings_text = archive.read("settings.txt").decode()
    assert "modulo_db = postgres" in settings_text
    assert "modulo_public_url = https://modulo.example" in settings_text
    assert "secret_key: set" in settings_text
    assert "modulo_users: set" in settings_text
    assert SETTINGS_SECRET not in settings_text


# ---------------------------------------------------------------------------
# (b) negative test — the fail-closed gate
# ---------------------------------------------------------------------------


def test_unclassified_secret_withholds_the_log_line_not_the_bundle(tmp_path: Path) -> None:
    """FAR-2333: one bad log line must not cost the whole bundle."""
    _seed_data_dir(tmp_path, f"harmless line\nGREETING={UNCLASSIFIED_SECRET}\n")
    out = tmp_path / "withheld.zip"

    result = build_bundle(
        out,
        sources=BundleSources(
            data_dir=tmp_path,
            doctor_output="healthy",
            settings=_FakeSettings(),
            environ={},
            enable_db=False,
        ),
    )

    assert out.is_file()
    with zipfile.ZipFile(out) as archive:
        log_text = archive.read("logs/app.log.tail").decode()
        report = json.loads(archive.read("report.json"))
        manifest = archive.read("MANIFEST.txt").decode()
    assert "harmless line" in log_text
    assert UNCLASSIFIED_SECRET not in log_text
    assert {
        "member": "logs/app.log.tail",
        "items": 1,
        "reason": "unclassified high-entropy token present",
        "scope": "item",
    } in report["withheld"]
    assert "withheld: logs/app.log.tail" in manifest
    assert any(entry["member"] == "logs/app.log.tail" for entry in result.withheld)


def test_cli_withholds_the_bad_log_line_and_writes_the_bundle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_data_dir(tmp_path, f"harmless line\nGREETING={UNCLASSIFIED_SECRET}\n")
    out = tmp_path / "withheld-cli.zip"
    monkeypatch.setattr(bundle_module, "_session_factory", lambda sources: None)

    code = main(["--out", str(out), "--data-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert code == 0
    assert str(out) in captured.out
    assert "withheld: logs/app.log.tail" in captured.out
    assert out.is_file()


# ---------------------------------------------------------------------------
# (c) golden member set
# ---------------------------------------------------------------------------


class _FakeRow:
    def __init__(self, mapping: dict[str, Any]) -> None:
        self._mapping = mapping

    def __getattr__(self, name: str) -> Any:
        return self._mapping[name]


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def scalar_one_or_none(self) -> Any:
        return self._rows[0]["id"] if self._rows else None

    def scalar_one(self) -> Any:
        return self._rows[0]["value"]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return [(row["value"],) for row in self._rows]

    def __iter__(self) -> Iterator[_FakeRow]:
        return iter(_FakeRow(row) for row in self._rows)


class _FakeBegin:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeSession:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def begin(self) -> _FakeBegin:
        return _FakeBegin()

    async def execute(self, statement: Any, params: Any = None) -> _FakeResult:
        sql = str(statement)
        if "count(*)" in sql:
            return _FakeResult([{"value": 7}])
        if "FROM organisations" in sql:
            return _FakeResult([{"id": "11111111-1111-1111-1111-111111111111"}])
        if "alembic_version" in sql:
            return _FakeResult([{"value": "0300_support_bundle"}])
        if "server_version" in sql:
            return _FakeResult([{"id": "16.4"}])
        if "FROM error_groups" in sql:
            return _FakeResult(
                [
                    {
                        "id": "22222222-2222-2222-2222-222222222222",
                        "fingerprint": "f" * 64,
                        "level_peak": "error",
                        "count": 3,
                        "first_seen": "2026-11-01T00:00:00+00:00",
                        "last_seen": "2026-11-02T00:00:00+00:00",
                        "level": "error",
                        "source": "backend",
                        "environment": "production",
                        "version": "0.3.323",
                        "message": "boom",
                    }
                ]
            )
        if "FROM runs" in sql:
            return _FakeResult(
                [
                    {
                        "id": "33333333-3333-3333-3333-333333333333",
                        "run_number": 12,
                        "pipeline_id": "44444444-4444-4444-4444-444444444444",
                        "pipeline_name": "dogfood",
                        "status": "failed",
                        "error_code": "harness.unknown",
                        "started_at": "2026-11-02T00:00:00+00:00",
                        "completed_at": "2026-11-02T00:01:00+00:00",
                        "created_at": "2026-11-02T00:00:00+00:00",
                        "total_cost_usd": "0.01",
                    }
                ]
            )
        return _FakeResult([])


async def _fake_set_rls_org(session: Any, org_id: Any) -> None:
    return None


def test_golden_member_set_is_exact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_data_dir(tmp_path, "clean log")
    monkeypatch.setattr("farna.db.rls.set_rls_org", _fake_set_rls_org)
    out = tmp_path / "golden.zip"
    build_bundle(
        out,
        sources=BundleSources(
            data_dir=tmp_path,
            doctor_output="healthy",
            settings=_FakeSettings(),
            environ={},
            session_factory=lambda: _FakeSession(),
        ),
    )
    with zipfile.ZipFile(out) as archive:
        names = set(archive.namelist())
        report = json.loads(archive.read("report.json"))
        manifest_text = archive.read("MANIFEST.txt").decode()
    expected = set(GOLDEN_MEMBERS) | {
        "logs/app.log.tail",
        "logs/postgres.log.tail",
        "logs/redis.log.tail",
    }
    assert names == expected
    assert {entry["name"] for entry in report["members"]} == names
    assert not report["missing"]
    # MANIFEST.txt lists EVERY member with its byte count, including itself.
    for name in names:
        assert f"  {name} |" in manifest_text
    with zipfile.ZipFile(out) as archive:
        for entry in report["members"]:
            assert len(archive.read(entry["name"])) == entry["bytes"]
    # Sanity: the DB members actually carry the canned inventory.
    with zipfile.ZipFile(out) as archive:
        assert "runs: 7" in archive.read("db.txt").decode()
        assert "0300_support_bundle" in archive.read("db.txt").decode()


# ---------------------------------------------------------------------------
# (d) --list / --show never print an unredacted value
# ---------------------------------------------------------------------------


def test_list_and_show_never_print_a_secret(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_data_dir(tmp_path, _planted_log())
    out = _build_full(tmp_path)
    capsys.readouterr()

    code = main(["--out", str(out), "--list"])
    listed = capsys.readouterr().out
    assert code == 0
    assert "logs/app.log.tail" in listed
    for secret in ALL_PLANTED:
        assert secret not in listed

    code = main(["--out", str(out), "--show", "logs/app.log.tail"])
    shown = capsys.readouterr().out
    assert code == 0
    for secret in ALL_PLANTED:
        assert secret not in shown
    assert REDACTED in shown


# ---------------------------------------------------------------------------
# (e) unknown-member forward compatibility
# ---------------------------------------------------------------------------


def test_reader_tolerates_unknown_members(tmp_path: Path) -> None:
    _seed_data_dir(tmp_path, "clean log")
    out = _build_full(tmp_path)
    with zipfile.ZipFile(out, "a") as archive:
        archive.writestr("FUTURE.txt", "written by a newer bundle format")

    assert is_allowed_member("FUTURE.txt") is False
    members = read_bundle(out)
    assert "FUTURE.txt" in members
    inventory = inspect_bundle(out)
    assert inventory.bundle_format_version == BUNDLE_FORMAT_VERSION
    assert ("FUTURE.txt", len(b"written by a newer bundle format")) in inventory.members


# ---------------------------------------------------------------------------
# (f) crash-loop log collapse
# ---------------------------------------------------------------------------


def test_crash_loop_log_is_collapsed_and_bounded(tmp_path: Path) -> None:
    repeated = "\n".join(["FATAL: postgres exited with code 1"] * 5000)
    _seed_data_dir(tmp_path, repeated)
    out = _build_full(tmp_path)
    with zipfile.ZipFile(out) as archive:
        text = archive.read("logs/app.log.tail").decode()
    lines = text.splitlines()
    assert len(lines) < 10
    assert any("repeated" in line for line in lines)


def test_oversized_bundle_truncates_logs_and_records_it(tmp_path: Path) -> None:
    import random

    rng = random.Random(2305)
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    noisy = "\n".join("".join(rng.choice(alphabet) for _ in range(80)) for _ in range(2000))
    _seed_data_dir(tmp_path, noisy)
    out = tmp_path / "small.zip"
    result = build_bundle(
        out,
        sources=BundleSources(
            data_dir=tmp_path,
            doctor_output="healthy",
            settings=_FakeSettings(),
            environ={},
            enable_db=False,
            max_bundle_bytes=4000,
        ),
    )
    assert result.truncated_log_members
    with zipfile.ZipFile(out) as archive:
        report = json.loads(archive.read("report.json"))
    assert report["truncated_log_members"]


# ---------------------------------------------------------------------------
# (g) doctor --report still produces a valid v1 bundle; --json shape
# ---------------------------------------------------------------------------


def test_doctor_report_delegates_to_v1_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from farna.launcher.doctor_report import build_report

    _seed_data_dir(tmp_path, _planted_log())
    monkeypatch.setattr(bundle_module, "_session_factory", lambda sources: None)
    out = build_report(tmp_path, tmp_path / "doctor.zip", doctor_output="doctor says healthy")

    with zipfile.ZipFile(out) as archive:
        names = set(archive.namelist())
        report = json.loads(archive.read("report.json"))
        doctor_text = archive.read("doctor.txt").decode()
    assert names >= {"MANIFEST.txt", "report.json", "version.txt", "doctor.txt"}
    assert report["bundle_format_version"] == BUNDLE_FORMAT_VERSION
    assert "doctor says healthy" in doctor_text
    assert POSTGRES_PASSWORD.encode() not in _member_blob(out)
    with zipfile.ZipFile(out) as archive:
        assert "bundle_format_version: 1" in archive.read("MANIFEST.txt").decode()


def test_cli_json_prints_one_object(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_data_dir(tmp_path, "clean log")
    out = tmp_path / "json.zip"
    monkeypatch.setattr(bundle_module, "_session_factory", lambda sources: None)

    code = main(["--out", str(out), "--data-dir", str(tmp_path), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["bundle_format_version"] == BUNDLE_FORMAT_VERSION
    assert payload["reference"] == "modulo-support-bundle-v1"
    assert payload["bundle_path"] == str(out)
    assert isinstance(payload["members"], list)
    assert isinstance(payload["redaction"], dict)
    # (a) the per-run ref is a token, distinct from the format identifier
    assert REF_SHAPE.fullmatch(str(payload["ref"]))
    assert str(payload["ref"]) in str(payload["ref_line"])


def test_ref_is_the_same_in_json_report_and_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b) one ref, stamped identically in the payload, report.json and MANIFEST."""
    _seed_data_dir(tmp_path, "clean log")
    out = tmp_path / "ref.zip"
    monkeypatch.setattr(bundle_module, "_session_factory", lambda sources: None)

    code = main(["--out", str(out), "--data-dir", str(tmp_path), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    with zipfile.ZipFile(out) as archive:
        report = json.loads(archive.read("report.json"))
        manifest = archive.read("MANIFEST.txt").decode()
    assert report["ref"] == payload["ref"]
    assert f"ref: {payload['ref']}" in manifest
    # the reader recovers the same token
    assert inspect_bundle(out).ref == payload["ref"]


def test_generate_ref_emits_the_shared_shape_and_is_unique() -> None:
    first = generate_ref()
    second = generate_ref()
    assert REF_SHAPE.fullmatch(first)
    assert first != second


def test_cli_prints_the_pasteable_ref_line_and_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_data_dir(tmp_path, "clean log")
    out = tmp_path / "printed.zip"
    monkeypatch.setattr(bundle_module, "_session_factory", lambda sources: None)

    code = main(["--out", str(out), "--data-dir", str(tmp_path)])

    printed = capsys.readouterr().out
    assert code == 0
    assert REF_SHAPE.fullmatch(_printed_ref(printed))
    assert str(out) in printed
    assert "bytes)" in printed
    assert "check it before sending" in printed


def _printed_ref(printed: str) -> str:
    for line in printed.splitlines():
        if "ref=" in line:
            return line.split("ref=", 1)[1].strip()
    return ""


def test_show_accepts_the_bundle_own_ref(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--show report.json` must not be refused by the gate on its own ref."""
    _seed_data_dir(tmp_path, "clean log")
    out = tmp_path / "show.zip"
    monkeypatch.setattr(bundle_module, "_session_factory", lambda sources: None)
    main(["--out", str(out), "--data-dir", str(tmp_path)])
    capsys.readouterr()

    code = main(["--out", str(out), "--show", "report.json"])

    shown = capsys.readouterr().out
    assert code == 0
    assert REF_SHAPE.fullmatch(str(json.loads(shown)["ref"]))


# ---------------------------------------------------------------------------
# (h) --redact-stdin: the collector's text mode, same scrubbers + gate
# ---------------------------------------------------------------------------


def _run_redact_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], payload: bytes
) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8"))
    code = main(["--redact-stdin"])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_redact_stdin_scrubs_token_and_url_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    text = (
        "\n".join(
            [
                f"pull failed with token {GITHUB_PAT}",
                "cache: redis://default:sup3rs3cret@cache.internal:6379/0",
                "keep this line",
            ]
        )
        + "\n"
    )

    code, out, _err = _run_redact_stdin(monkeypatch, capsys, text.encode("utf-8"))

    assert code == 0
    assert GITHUB_PAT not in out
    assert "sup3rs3cret" not in out
    assert "keep this line" in out
    # a clean trailing newline survives (the collector may concatenate streams)
    assert out.endswith("\n")


def test_redact_stdin_fails_closed_on_unclassified_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run_redact_stdin(monkeypatch, capsys, f"GREETING={UNCLASSIFIED_SECRET}\n".encode())

    assert code != 0
    assert not out
    assert "high-entropy" in err


def test_redact_stdin_mode_is_documented(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])

    assert "--redact-stdin" in capsys.readouterr().out
