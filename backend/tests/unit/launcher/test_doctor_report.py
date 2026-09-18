"""Unit tests for ``modulo doctor --report`` (FAR-676).

The redaction contract is the core of the suite: the zip is seeded with
REAL-LOOKING generated credentials in the secrets file, those values appear
in the archived log/doctor text, and the test greps every member of the
resulting archive proving NO secret value survives while the
``<redacted>`` marker DOES.
"""

import json
import zipfile
from pathlib import Path

import pytest

from modulo.launcher.doctor_report import (
    REDACTED,
    build_report,
    redact_text,
    redaction_map_from_data_dir,
)

POSTGRES_PASSWORD = "Xk9!pQz7-weird-real-looking-pg-secret"
REDIS_PASSWORD = "Lq2#vN8-real-redis-cookie-value-77"
HMAC_KEY_HEX = "b2c4" * 16


def _seed_data_dir(tmp_path: Path) -> None:
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
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (tmp_path / "launcher.log").write_text(
        f"boot ok; url=postgresql://modulo:{POSTGRES_PASSWORD}@127.0.0.1:15432/postgres "
        f"redis://:{REDIS_PASSWORD}:16379/0\n",
        encoding="utf-8",
    )
    (log_dir / "postgres.log").write_text(
        f"FATAL password authentication failed for user modulo (tried {POSTGRES_PASSWORD}) mac-key={HMAC_KEY_HEX}\n",
        encoding="utf-8",
    )
    (log_dir / "redis.log").write_text(f"requirepass {REDIS_PASSWORD}\n", encoding="utf-8")


def test_redaction_map_seeded_from_secrets(tmp_path: Path) -> None:
    _seed_data_dir(tmp_path)
    mapping = redaction_map_from_data_dir(tmp_path)
    assert POSTGRES_PASSWORD in mapping
    assert REDIS_PASSWORD in mapping
    assert HMAC_KEY_HEX in mapping
    assert all(value == REDACTED for value in mapping.values())


def test_redaction_map_empty_without_secrets(tmp_path: Path) -> None:
    assert not redaction_map_from_data_dir(tmp_path)


def test_redaction_map_scrubs_percent_encoded_values(tmp_path: Path) -> None:
    """Credentials embedded in URLs are percent-encoded (e.g. ``p@ss`` -> ``p%40ss``);
    the seed map must also scrub that encoded form so the value never survives."""
    from urllib.parse import quote

    password = "p@ssword"
    (tmp_path / "secrets.json").write_text(
        json.dumps(
            {
                "postgres_password": password,
                "redis_password": "r",
                "state_hmac_key": HMAC_KEY_HEX,
            }
        ),
        encoding="utf-8",
    )
    mapping = redaction_map_from_data_dir(tmp_path)
    assert password in mapping
    assert quote(password) in mapping  # percent-encoded variant is also a scrub pattern
    encoded_url = f"redis://:{quote(password)}@127.0.0.1:6379/0"
    redacted = redact_text(encoded_url, mapping)
    assert password not in redacted
    assert quote(password) not in redacted
    assert REDACTED in redacted


def test_redact_text_masks_seeded_values_and_sensitive_kv() -> None:
    secret_values = {"pw-super-secret": REDACTED}
    text = "\n".join(
        [
            "postgres password pw-super-secret leaked",
            "ALERT_WEBHOOK_URL=https://hooks.example.com/tokenzAbCdEf123",
            "PLAIN_VALUE=forty-two",
        ]
    )
    redacted = redact_text(text, secret_values)
    assert "pw-super-secret" not in redacted
    # webhook URLs carry the credential in the path (wholesale redaction, as modulo env does)
    assert f"ALERT_WEBHOOK_URL={REDACTED}" in redacted
    assert "PLAIN_VALUE=forty-two" in redacted


def test_report_zip_contains_no_secret_value(tmp_path: Path) -> None:
    _seed_data_dir(tmp_path)
    doctor_output = (
        "modulo doctor — data dir: <dir>\n"
        f"  [ok  ] postgres password diagnostic {POSTGRES_PASSWORD}\n"
        f"  [ok  ] redis {REDIS_PASSWORD}\n"
    )
    out_path = build_report(tmp_path, tmp_path / "report.zip", doctor_output=doctor_output)
    with zipfile.ZipFile(out_path) as archive:
        names = archive.namelist()
        blob = b"".join(archive.read(member) for member in names)
    text = blob.decode("utf-8", errors="replace")
    for secret in (POSTGRES_PASSWORD, REDIS_PASSWORD, HMAC_KEY_HEX):
        assert secret.encode() not in blob, f"secret {secret!r} leaked into the report archive"
    assert REDACTED in text
    # the secrets file itself must never be archived
    assert "secrets.json" not in names
    assert "logs/secrets.json.tail" not in names


def test_report_zip_inventory_and_versions(tmp_path: Path) -> None:
    _seed_data_dir(tmp_path)
    out_path = build_report(tmp_path, tmp_path / "report.zip", doctor_output="healthy")
    with zipfile.ZipFile(out_path) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("report.json"))
        version_text = archive.read("version.txt").decode()
    assert "doctor-output.txt" in names
    assert "logs/app.log.tail" in names
    assert "logs/postgres.log.tail" in names
    assert "logs/redis.log.tail" in names
    assert manifest["generated_by"] == "modulo doctor --report"
    assert manifest["redacted_marker"] == REDACTED
    assert "modulo version:" in version_text
    assert "os:" in version_text


def test_report_marks_absent_logs(tmp_path: Path) -> None:
    """A data dir with no logs still builds an empty-tail report."""
    out_path = build_report(tmp_path, tmp_path / "report.zip", doctor_output="uninitialized")
    with zipfile.ZipFile(out_path) as archive:
        assert not archive.read("logs/app.log.tail").decode()


def test_build_failure_secrets_unreadable_still_redacts_structurally(tmp_path: Path) -> None:
    (tmp_path / "secrets.json").write_text("}not json{", encoding="utf-8")
    doctor_output = f"SECRET_KEY={chr(115) * 12}value\n"
    out_path = build_report(tmp_path, tmp_path / "report.zip", doctor_output=doctor_output)
    with zipfile.ZipFile(out_path) as archive:
        text = archive.read("doctor-output.txt").decode()
    assert f"SECRET_KEY={REDACTED}" in text


# ---------------------------------------------------------------------------
# kept a small seam for future parametrization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bytes_budget", [16, 256])
def test_tail_budget_brings_back_few_bytes(tmp_path: Path, bytes_budget: int) -> None:
    path = tmp_path / "log"
    path.write_text("a" * 1_000_000, encoding="utf-8")
    from modulo.launcher.supervisor import read_log_tail

    tail = read_log_tail(path, max_bytes=bytes_budget)
    assert len(tail) == bytes_budget
