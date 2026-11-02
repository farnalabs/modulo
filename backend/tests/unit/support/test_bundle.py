"""Support-bundle format v1: allowlist, inventory, redaction gate, reader (FAR-2305).

Every planted credential is obviously fake. The fixture seeds a native data dir
with the real generated-secret shapes plus planted tokens in every log, builds
the bundle, and greps every member proving no planted value survives.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import zipfile
from pathlib import Path

import pytest

import modulo.support.bundle as bundle_module
from modulo.support.bundle import (
    BUNDLE_FORMAT_VERSION,
    FIXED_MEMBERS,
    FORMAT_ID,
    MAX_BUNDLE_COMPRESSED_BYTES,
    BundleBuildError,
    BundleResult,
    build_bundle,
    list_bundle,
    main,
    show_member,
)
from modulo.support.collect import (
    BundleSources,
    collect_async_members,
    collect_sources,
)
from modulo.support.redaction import (
    REDACTED,
    RedactionCounts,
    RedactionResult,
    RedactionSelfCheckError,
    assert_redaction_proven,
)

PG_PASSWORD = "Xk9!pQz7-weird-real-looking-pg-secret"
REDIS_PASSWORD = "Lq2#vN8-real-redis-cookie-value-77"
HMAC_KEY_HEX = "b2c4" * 16
GITHUB_PAT = "ghp_" + "B" * 36
OPENAI_KEY = "sk-" + "c" * 26
BEARER_TOKEN = "Bearer fAkE-token-value-abcdef"

EXPECTED_GOLDEN_MEMBERS = {
    "MANIFEST.txt",
    "report.json",
    "version.txt",
    "doctor.txt",
    "health.json",
    "settings.txt",
    "deployment.txt",
    "db.txt",
    "errors.json",
    "runs.json",
    "licence.txt",
    "degraded.json",
}


def _seed_native_dir(tmp_path: Path) -> None:
    (tmp_path / "secrets.json").write_text(
        json.dumps(
            {
                "postgres_password": PG_PASSWORD,
                "redis_password": REDIS_PASSWORD,
                "state_hmac_key": HMAC_KEY_HEX,
            }
        ),
        encoding="utf-8",
    )
    crash_loop = "\n".join(["crash loop tick"] * 500)
    (tmp_path / "launcher.log").write_text(
        "\n".join(
            [
                f"boot pg={PG_PASSWORD} redis={REDIS_PASSWORD} hmac={HMAC_KEY_HEX}",
                f"auth github {GITHUB_PAT}",
                f"model key {OPENAI_KEY}",
                f"header {BEARER_TOKEN}",
                f"redis://modulo:{REDIS_PASSWORD}@127.0.0.1:16379/0",
                "SECRET_KEY=fake-structural-secret",
                crash_loop,
                "",
            ]
        ),
        encoding="utf-8",
    )
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "postgres.log").write_text(
        f"FATAL password authentication failed (tried {PG_PASSWORD})\n", encoding="utf-8"
    )
    (log_dir / "redis.log").write_text(f"requirepass {REDIS_PASSWORD}\n", encoding="utf-8")
    (tmp_path / "degraded.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "degraded_at": 1767225600.0,
                "reason": f"postgres exited after using {PG_PASSWORD}",
                "crashes": [
                    {
                        "child": "postgres",
                        "exit_code": 1,
                        "at": 1767225600.0,
                        "backtrace": f"boom {GITHUB_PAT}",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _full_sources(tmp_path: Path) -> BundleSources:
    _seed_native_dir(tmp_path)
    sources = collect_sources(
        install_shape="native",
        data_dir=tmp_path,
        doctor_output=f"doctor: connectivity ok (key {OPENAI_KEY})\n",
        collect_async=False,
    )
    sources.members.update(
        {
            "health.json": json.dumps({"status": "ok", "checks": [{"name": "database", "status": "ok"}]}),
            "db.txt": (
                "dialect: postgresql\n"
                "migration_head: 0334\n"
                "expected_head: 0334\n"
                "server_version: 16.4\n"
                "row_counts:\n"
                "  runs: 12\n"
            ),
            "errors.json": json.dumps(
                {
                    "error_groups": [
                        {
                            "id": "00000000-0000-0000-0000-000000000001",
                            "fingerprint": "abc123",
                            "level": "error",
                            "source": "backend",
                            "count": 3,
                            "message": "connection refused",
                            "omitted": ["context_json", "stacktrace"],
                        }
                    ]
                }
            ),
            "runs.json": json.dumps(
                {
                    "failed_runs": [
                        {
                            "run_id": "00000000-0000-0000-0000-000000000002",
                            "pipeline_id": "00000000-0000-0000-0000-000000000003",
                            "pipeline_name": "nightly",
                            "status": "failed",
                            "error_code": "harness.executor_failed",
                        }
                    ]
                }
            ),
            "licence.txt": (
                "tier: community\n"
                "state: none\n"
                "expires_at: None\n"
                "licence_id: None\n"
                "renewal_mode: None\n"
                "features: \n"
                "enabled_flags: eval_system\n"
                "licence_key: never included\n"
            ),
        }
    )
    for name in ("health.json", "db.txt", "errors.json", "runs.json", "licence.txt"):
        sources.missing.pop(name, None)
    return sources


def _build(tmp_path: Path, sources: BundleSources | None = None) -> tuple[BundleResult, dict[str, str]]:
    out_path = tmp_path / "bundle.zip"
    result = build_bundle(out_path, sources=sources or _full_sources(tmp_path), generated_by="pytest")
    with zipfile.ZipFile(out_path) as archive:
        members = {name: archive.read(name).decode("utf-8") for name in archive.namelist()}
    return result, members


def test_golden_member_allowlist_is_exact() -> None:
    assert set(FIXED_MEMBERS) == EXPECTED_GOLDEN_MEMBERS
    assert len(FIXED_MEMBERS) == len(EXPECTED_GOLDEN_MEMBERS)


def test_every_built_member_is_allowlisted(tmp_path: Path) -> None:
    _result, members = _build(tmp_path)
    for name in members:
        assert name in FIXED_MEMBERS or (name.startswith("logs/") and name.endswith(".log.tail"))
    for required in ("MANIFEST.txt", "report.json", "version.txt", "doctor.txt", "degraded.json"):
        assert required in members


def test_format_version_is_stamped_in_report_manifest_and_ref_line(tmp_path: Path) -> None:
    result, members = _build(tmp_path)
    report = json.loads(members["report.json"])
    assert report["format"] == FORMAT_ID
    assert report["bundle_format_version"] == BUNDLE_FORMAT_VERSION
    assert FORMAT_ID in members["MANIFEST.txt"]
    assert FORMAT_ID in result.ref_line
    assert result.path.is_file()


def test_manifest_lists_every_member_and_report_bytes_match(tmp_path: Path) -> None:
    _result, members = _build(tmp_path)
    manifest = members["MANIFEST.txt"]
    for name in members:
        assert name in manifest
    report = json.loads(members["report.json"])
    for entry in report["members"]:
        assert entry["bytes"] == len(members[entry["name"]].encode("utf-8"))


def test_no_planted_secret_survives_in_any_member(tmp_path: Path) -> None:
    _result, members = _build(tmp_path)
    for secret in (PG_PASSWORD, REDIS_PASSWORD, HMAC_KEY_HEX, GITHUB_PAT, OPENAI_KEY, BEARER_TOKEN):
        for name, text in members.items():
            assert secret not in text, f"{secret!r} leaked into {name}"
    assert "secrets.json" not in members
    report = json.loads(members["report.json"])
    layers = report["redaction"]["layers"]
    assert layers["seed_exact"] > 0
    assert layers["structural"] > 0
    assert layers["patterns"] > 0
    assert set(report["redaction"]["layers_applied"]) == {"seed_exact", "structural", "patterns"}
    assert report["redaction"]["redacted_marker"] == REDACTED


def test_build_refuses_when_redaction_is_bypassed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prove-the-fix: the build wires the gate; a bypassed scrub aborts the build."""
    sources = BundleSources(
        install_shape="container",
        members={"version.txt": "modulo version: test", "doctor.txt": f"planted {PG_PASSWORD}"},
        secret_values={PG_PASSWORD: REDACTED},
    )
    monkeypatch.setattr(
        bundle_module,
        "redact_text",
        lambda text, _secrets: RedactionResult(text=text, counts=RedactionCounts()),
    )
    out_path = tmp_path / "refused.zip"
    with pytest.raises(RedactionSelfCheckError) as caught:
        build_bundle(out_path, sources=sources)
    assert caught.value.member == "doctor.txt"
    assert not out_path.exists()


def test_build_rejects_a_member_outside_the_allowlist(tmp_path: Path) -> None:
    sources = BundleSources(
        install_shape="container",
        members={"version.txt": "modulo version: test", "future-member.txt": "nope"},
    )
    out_path = tmp_path / "refused.zip"
    with pytest.raises(BundleBuildError):
        build_bundle(out_path, sources=sources)
    assert not out_path.exists()


def test_list_and_show_work(tmp_path: Path) -> None:
    _result, members = _build(tmp_path)
    entries = list_bundle(tmp_path / "bundle.zip")
    assert {entry.name for entry in entries} == set(members)
    assert all(entry.known for entry in entries)
    shown = show_member(tmp_path / "bundle.zip", "MANIFEST.txt")
    assert shown == members["MANIFEST.txt"]
    assert FORMAT_ID in shown


def test_reader_tolerates_unknown_members(tmp_path: Path) -> None:
    _result, members = _build(tmp_path)
    future = tmp_path / "future.zip"
    with zipfile.ZipFile(tmp_path / "bundle.zip") as source, zipfile.ZipFile(future, "w") as target:
        for info in source.infolist():
            target.writestr(info.filename, source.read(info.filename))
        target.writestr("future-member.txt", "a v2 member")
    entries = list_bundle(future)
    unknown = [entry for entry in entries if entry.name == "future-member.txt"]
    assert len(unknown) == 1
    assert not unknown[0].known
    assert show_member(future, "future-member.txt") == "a v2 member"
    assert set(members) <= {entry.name for entry in entries}


def test_show_refuses_a_tampered_secret(tmp_path: Path) -> None:
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(tampered, "w") as archive:
        archive.writestr("doctor.txt", f"token {GITHUB_PAT}")
    with pytest.raises(RedactionSelfCheckError):
        show_member(tampered, "doctor.txt")


def test_crash_loop_log_is_collapsed(tmp_path: Path) -> None:
    _result, members = _build(tmp_path)
    tail = members["logs/app.log.tail"]
    assert len(tail.splitlines()) < 20
    assert "repeated" in tail


def test_bundle_is_under_the_compressed_cap(tmp_path: Path) -> None:
    result, _members = _build(tmp_path)
    assert result.compressed_bytes <= MAX_BUNDLE_COMPRESSED_BYTES


def test_missing_members_are_recorded_honestly(tmp_path: Path) -> None:
    sources = collect_sources(install_shape="container", collect_async=False)
    result = build_bundle(tmp_path / "container.zip", sources=sources)
    with zipfile.ZipFile(tmp_path / "container.zip") as archive:
        manifest = archive.read("MANIFEST.txt").decode()
        report = json.loads(archive.read("report.json"))
        names = set(archive.namelist())
    assert "version.txt" in names
    assert "deployment.txt" in names
    assert "missing: [" in manifest
    for name in ("health.json", "db.txt", "errors.json", "runs.json"):
        assert name in report["missing"]
    assert "degraded.json" in report["missing"]
    assert result.missing


def test_db_unreachable_still_emits_the_other_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _no_settings(**_kwargs: object) -> object:
        raise RuntimeError("no database configured")

    monkeypatch.setattr("modulo.settings.get_settings", _no_settings)
    sources = collect_sources(install_shape="container", collect_async=True)
    assert "version.txt" in sources.members
    assert "deployment.txt" in sources.members
    for name in ("db.txt", "errors.json", "runs.json"):
        assert name in sources.missing


def test_collect_async_members_marks_db_members_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _no_settings(**_kwargs: object) -> object:
        raise RuntimeError("no database configured")

    monkeypatch.setattr("modulo.settings.get_settings", _no_settings)
    collected = asyncio.run(collect_async_members())
    for name in ("db.txt", "errors.json", "runs.json"):
        assert name in collected.missing


def test_health_member_carries_no_free_text_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    import modulo.api.routes.health as health_module

    class _Check:
        status = "degraded"
        detail = "customer content that must never ship"
        latency_ms = 12.5

    class _Response:
        from typing import ClassVar

        status = "degraded"
        checks: ClassVar[dict[str, _Check]] = {"database": _Check()}

    async def _fake_readiness() -> _Response:
        return _Response()

    def _no_settings(**_kwargs: object) -> object:
        raise RuntimeError("no database configured")

    monkeypatch.setattr(health_module, "evaluate_readiness", _fake_readiness)
    monkeypatch.setattr("modulo.settings.get_settings", _no_settings)
    collected = asyncio.run(collect_async_members())
    assert collected.health is not None
    assert collected.health["checks"] == [{"name": "database", "status": "degraded"}]
    serialized = json.dumps(collected.health)
    assert "customer content" not in serialized
    assert "12.5" not in serialized


def test_size_cap_truncates_logs_and_records_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bundle_module, "MAX_BUNDLE_COMPRESSED_BYTES", 20_000)
    lines = "\n".join(f"tick {index} {hashlib.sha256(str(index).encode()).hexdigest()}" for index in range(8000))
    sources = BundleSources(
        install_shape="native",
        members={"version.txt": "modulo version: test", "logs/app.log.tail": lines},
    )
    result = build_bundle(tmp_path / "capped.zip", sources=sources)
    assert result.compressed_bytes <= 20_000
    assert "logs/app.log.tail" in result.truncated
    assert "bundle cap" in result.truncated["logs/app.log.tail"]


def test_cli_build_list_show_and_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sources = _full_sources(tmp_path)
    monkeypatch.setattr(bundle_module, "collect_sources", lambda **_kwargs: sources)

    out_path = tmp_path / "cli.zip"
    assert main(["--out", str(out_path)]) == 0
    assert FORMAT_ID in capsys.readouterr().out

    assert main(["--list", str(out_path)]) == 0
    listed = capsys.readouterr().out
    assert "MANIFEST.txt" in listed

    assert main(["--show", "MANIFEST.txt", str(out_path)]) == 0
    assert FORMAT_ID in capsys.readouterr().out

    assert main(["--out", str(tmp_path / "cli2.zip"), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["format"] == FORMAT_ID
    assert payload["bundle_format_version"] == BUNDLE_FORMAT_VERSION


def test_cli_refuses_and_emits_json_error_when_gate_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sources = BundleSources(
        install_shape="container",
        members={"version.txt": "modulo version: test", "doctor.txt": f"leak {PG_PASSWORD}"},
        secret_values={PG_PASSWORD: REDACTED},
    )
    monkeypatch.setattr(bundle_module, "collect_sources", lambda **_kwargs: sources)
    monkeypatch.setattr(
        bundle_module,
        "redact_text",
        lambda text, _secrets: RedactionResult(text=text, counts=RedactionCounts()),
    )
    out_path = tmp_path / "refused.zip"
    assert main(["--out", str(out_path), "--json"]) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["ok"] is False
    assert error["member"] == "doctor.txt"
    assert not out_path.exists()


# ---------------------------------------------------------------------------
# FAR-2305 review follow-up: the gate must judge COLLECTED content, not the
# bundle's own inventory, and a common-word password must not block the
# documented quickstart.
# ---------------------------------------------------------------------------

#: The value the repo's own quickstart documents (docs/quickstart.md).
QUICKSTART_PASSWORD = "modulo"
QUICKSTART_DATABASE_URL = f"postgresql+asyncpg://modulo_app:{QUICKSTART_PASSWORD}@db:5432/modulo"


def test_documented_quickstart_password_builds_a_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: `POSTGRES_PASSWORD=modulo` used to refuse every bundle.

    The word appears throughout the bundle's own inventory boilerplate, so the
    post-assembly gate reported a surviving secret and wrote no zip.
    """
    monkeypatch.setenv("POSTGRES_PASSWORD", QUICKSTART_PASSWORD)
    monkeypatch.setenv("DATABASE_URL", QUICKSTART_DATABASE_URL)
    sources = collect_sources(install_shape="container", collect_async=False)
    assert QUICKSTART_PASSWORD in sources.secret_values

    out_path = tmp_path / "quickstart.zip"
    result = build_bundle(out_path, sources=sources)
    assert out_path.is_file()
    assert "MANIFEST.txt" in result.members

    with zipfile.ZipFile(out_path) as archive:
        version = archive.read("version.txt").decode()
        manifest = archive.read("MANIFEST.txt").decode()
        report = json.loads(archive.read("report.json"))
    # Resource names are not mangled by a seed value that is also an English word.
    assert "modulo version:" in version
    assert "modulo support bundle" in manifest
    assert report["format"] == FORMAT_ID


def test_common_word_password_in_a_credential_context_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The weak-value tier must not weaken the gate: a real log leak still fails."""
    sources = BundleSources(
        install_shape="container",
        members={
            "version.txt": "modulo version: test",
            "logs/redis.log.tail": f"requirepass {QUICKSTART_PASSWORD}",
        },
        secret_values={QUICKSTART_PASSWORD: REDACTED},
    )
    monkeypatch.setattr(
        bundle_module,
        "redact_text",
        lambda text, _secrets: RedactionResult(text=text, counts=RedactionCounts()),
    )
    out_path = tmp_path / "refused.zip"
    with pytest.raises(RedactionSelfCheckError) as caught:
        build_bundle(out_path, sources=sources)
    assert caught.value.member == "logs/redis.log.tail"
    assert "credential context" in caught.value.reason
    assert not out_path.exists()


def test_common_word_password_is_scrubbed_out_of_a_real_log(tmp_path: Path) -> None:
    """With the seed layer active, the same leak is removed rather than refused."""
    sources = BundleSources(
        install_shape="container",
        members={"logs/redis.log.tail": f"requirepass {QUICKSTART_PASSWORD}"},
        secret_values={QUICKSTART_PASSWORD: REDACTED},
    )
    result = build_bundle(tmp_path / "ok.zip", sources=sources)
    with zipfile.ZipFile(result.path) as archive:
        tail = archive.read("logs/redis.log.tail").decode()
    assert f"requirepass {QUICKSTART_PASSWORD}" not in tail
    assert f"requirepass {REDACTED}" in tail
    assert result.redaction.seed_exact >= 1


def test_quoted_credential_is_scrubbed_out_of_a_real_json_log(tmp_path: Path) -> None:
    """FAR-2305 review: quoting/JSON between key and value must not hide the seed."""
    sources = BundleSources(
        install_shape="container",
        members={
            "version.txt": "modulo version: test",
            "logs/app.log.tail": 'loaded config {"POSTGRES_PASSWORD":"modulo"} ok',
        },
        secret_values={QUICKSTART_PASSWORD: REDACTED},
    )
    result = build_bundle(tmp_path / "quoted.zip", sources=sources)
    with zipfile.ZipFile(result.path) as archive:
        tail = archive.read("logs/app.log.tail").decode()
    assert QUICKSTART_PASSWORD not in tail
    assert f'"POSTGRES_PASSWORD":"{REDACTED}"' in tail
    assert result.redaction.seed_exact >= 1


def test_build_refuses_a_quoted_credential_when_the_seed_layer_is_bypassed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prove-the-fix for the quoted/JSON shapes, not only the plain `value` shape."""
    sources = BundleSources(
        install_shape="container",
        members={
            "version.txt": "modulo version: test",
            "logs/postgres.log.tail": 'FATAL: {"password": "modulo"} rejected',
        },
        secret_values={QUICKSTART_PASSWORD: REDACTED},
    )
    monkeypatch.setattr(
        bundle_module,
        "redact_text",
        lambda text, _secrets: RedactionResult(text=text, counts=RedactionCounts()),
    )
    out_path = tmp_path / "refused-quoted.zip"
    with pytest.raises(RedactionSelfCheckError) as caught:
        build_bundle(out_path, sources=sources)
    assert caught.value.member == "logs/postgres.log.tail"
    assert "credential context" in caught.value.reason
    assert not out_path.exists()


def test_inventory_is_gated_for_shapes_not_for_seeded_boilerplate() -> None:
    """The two gate calls have different scopes, on purpose."""
    seed = {QUICKSTART_PASSWORD: REDACTED}
    # Collected content: a bait word in boilerplate is not a leak...
    assert_redaction_proven({"version.txt": "modulo version: 0.3.323"}, seed, member_allowed=_allow_all)
    # ...but the same word next to a credential key is.
    with pytest.raises(RedactionSelfCheckError):
        assert_redaction_proven({"version.txt": "requirepass modulo"}, seed, member_allowed=_allow_all)
    # The inventory is gated with an empty seed map but keeps the shape checks.
    with pytest.raises(RedactionSelfCheckError):
        assert_redaction_proven({"report.json": f'{{"leak": "{GITHUB_PAT}"}}'}, {}, member_allowed=_allow_all)


def _allow_all(_name: str) -> bool:
    return True


# ---------------------------------------------------------------------------
# Remaining format-layer branches
# ---------------------------------------------------------------------------


def test_build_refuses_a_reserved_inventory_member(tmp_path: Path) -> None:
    sources = BundleSources(install_shape="container", members={"MANIFEST.txt": "hand-written"})
    with pytest.raises(BundleBuildError) as caught:
        build_bundle(tmp_path / "refused.zip", sources=sources)
    assert "reserved for the bundle inventory" in str(caught.value)


def test_build_fails_loudly_when_the_archive_cannot_shrink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bundle_module, "MAX_BUNDLE_COMPRESSED_BYTES", 1)
    sources = BundleSources(install_shape="container", members={"version.txt": "modulo version: test"})
    with pytest.raises(bundle_module.BundleSizeExceededError):
        build_bundle(tmp_path / "too-big.zip", sources=sources)
    assert not (tmp_path / "too-big.zip").exists()


def test_failed_write_leaves_no_partial_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(_self: Path, _target: Path) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", _fail)
    sources = BundleSources(install_shape="container", members={"version.txt": "modulo version: test"})
    with pytest.raises(OSError, match="disk full"):
        build_bundle(tmp_path / "out.zip", sources=sources)
    assert not (tmp_path / "out.zip").exists()
    assert not list(tmp_path.glob("*.tmp-*"))


def test_build_fails_when_the_inventory_never_stabilises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    counter = iter(range(1000))
    monkeypatch.setattr(bundle_module, "_manifest_text", lambda *_args, **_kwargs: f"manifest {next(counter)}")
    sources = BundleSources(install_shape="container", members={"version.txt": "modulo version: test"})
    with pytest.raises(BundleBuildError) as caught:
        build_bundle(tmp_path / "unstable.zip", sources=sources)
    assert "did not reach a stable size" in str(caught.value)


def test_unknown_allowlisted_member_has_no_description() -> None:
    with pytest.raises(BundleBuildError) as caught:
        bundle_module._description_for("future-member.txt")
    assert "no support-bundle v1 description" in str(caught.value)


def test_show_refuses_a_member_that_is_not_in_the_bundle(tmp_path: Path) -> None:
    _result, _members = _build(tmp_path)
    with pytest.raises(BundleBuildError) as caught:
        show_member(tmp_path / "bundle.zip", "nope.txt")
    assert "not in" in str(caught.value)


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--out", "x.zip", "--list", "y.zip"],
        ["--list"],
    ],
)
def test_cli_argument_errors_exit_two(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        main(argv)
    assert caught.value.code == 2


def test_cli_list_json_and_read_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sources = _full_sources(tmp_path)
    monkeypatch.setattr(bundle_module, "collect_sources", lambda **_kwargs: sources)
    out_path = tmp_path / "cli.zip"
    assert main(["--out", str(out_path)]) == 0
    capsys.readouterr()

    assert main(["--list", "--json", str(out_path)]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert isinstance(listed, list)
    assert all(entry["known"] for entry in listed)

    broken = tmp_path / "broken.zip"
    broken.write_text("not a zip", encoding="utf-8")
    assert main(["--list", str(broken)]) == 1
    plain_error = capsys.readouterr().err
    assert "zip" in plain_error.lower()

    assert main(["--list", "--json", str(broken)]) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["ok"] is False
    assert error["error"] == "BadZipFile"

    assert main(["--show", "nope.txt", str(out_path)]) == 1
    assert "not in" in capsys.readouterr().err


def test_cli_reads_doctor_output_and_prints_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sources = BundleSources(install_shape="container", members={"version.txt": "modulo version: test"})
    sources.missing["db.txt"] = "database inventory unavailable (RuntimeError)"
    monkeypatch.setattr(bundle_module, "collect_sources", lambda **_kwargs: sources)
    doctor_file = tmp_path / "doctor.txt"
    doctor_file.write_text("doctor: ok\n", encoding="utf-8")

    out_path = tmp_path / "cli.zip"
    assert main(["--out", str(out_path), "--doctor-output", str(doctor_file)]) == 0
    printed = capsys.readouterr().out
    assert FORMAT_ID in printed
    assert "missing: [db.txt]" in printed


def test_cli_prints_a_plain_error_when_the_gate_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sources = BundleSources(
        install_shape="container",
        members={"doctor.txt": f"leak {PG_PASSWORD}"},
        secret_values={PG_PASSWORD: REDACTED},
    )
    monkeypatch.setattr(bundle_module, "collect_sources", lambda **_kwargs: sources)
    monkeypatch.setattr(
        bundle_module,
        "redact_text",
        lambda text, _secrets: RedactionResult(text=text, counts=RedactionCounts()),
    )
    out_path = tmp_path / "refused.zip"
    assert main(["--out", str(out_path)]) == 1
    assert "redaction self-check failed" in capsys.readouterr().err


def test_degraded_redaction_layer_is_visible_in_the_artefact(tmp_path: Path) -> None:
    _result, members = _build(tmp_path)
    report = json.loads(members["report.json"])
    assert not report["degraded_layers"]
    assert "degraded layers: []" in members["MANIFEST.txt"]


def test_build_report_records_an_unreadable_secrets_file_as_degraded(tmp_path: Path) -> None:
    (tmp_path / "secrets.json").write_text("}not json{", encoding="utf-8")
    out_path = tmp_path / "degraded.zip"
    build_bundle(
        out_path,
        sources=collect_sources(
            install_shape="native", data_dir=tmp_path, doctor_output="doctor: ok", collect_async=False
        ),
    )
    with zipfile.ZipFile(out_path) as archive:
        report = json.loads(archive.read("report.json"))
        manifest = archive.read("MANIFEST.txt").decode()
    assert "seed_exact" in report["degraded_layers"]
    assert "unreadable" in report["degraded_layers"]["seed_exact"]
    assert "degraded layers: [seed_exact]" in manifest


def test_cli_prints_no_missing_line_when_nothing_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sources = BundleSources(install_shape="container", members={"version.txt": "modulo version: test"})
    monkeypatch.setattr(bundle_module, "collect_sources", lambda **_kwargs: sources)
    out_path = tmp_path / "clean.zip"
    assert main(["--out", str(out_path)]) == 0
    printed = capsys.readouterr().out
    assert FORMAT_ID in printed
    assert "missing:" not in printed


def test_weak_seeded_password_is_scrubbed_from_a_real_log_when_seeding_works(tmp_path: Path) -> None:
    """Seed-exact still catches the credential in a genuine log member."""
    (tmp_path / "secrets.json").write_text(
        json.dumps(
            {
                "postgres_password": QUICKSTART_PASSWORD,
                "redis_password": QUICKSTART_PASSWORD,
                "state_hmac_key": "b2c4" * 16,
            }
        ),
        encoding="utf-8",
    )
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "redis.log").write_text(f"requirepass {QUICKSTART_PASSWORD}\n", encoding="utf-8")

    sources = collect_sources(
        install_shape="native", data_dir=tmp_path, doctor_output="doctor: ok", collect_async=False
    )
    assert QUICKSTART_PASSWORD in sources.secret_values

    result = build_bundle(tmp_path / "native.zip", sources=sources)
    with zipfile.ZipFile(result.path) as archive:
        tail = archive.read("logs/redis.log.tail").decode()
        report = json.loads(archive.read("report.json"))
    assert f"requirepass {QUICKSTART_PASSWORD}" not in tail
    assert f"requirepass {REDACTED}" in tail
    assert report["redaction"]["layers"]["seed_exact"] >= 1
    assert not report["degraded_layers"]
