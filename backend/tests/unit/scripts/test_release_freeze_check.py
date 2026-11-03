"""Unit tests for .github/scripts/release-freeze-check.py.

These prove the shared release-freeze gate used by both the merge-queue
`freeze-gate` job and the pre-merge TOCTOU re-verify step:
  * queries the `release-freeze` branch namespace, not the tag namespace,
  * detects an active freeze (branch present) on the first successful response,
  * treats a missing branch as open (gate clear),
  * retries transient gh failures/blips and only FAILS CLOSED once every attempt
    is exhausted (so a single 503 cannot abort the tick or lift the freeze),
  * skips the backoff sleep on the final attempt (no pointless trailing delay),
  * surfaces the captured gh stderr in the fail-closed message so a persistent
    outage is diagnosable, and
  * FAILS CLOSED on any unverifiable response (never fails open).

FAR-2326 additionally proves the marker detail and the stale flag:
  * the verdict stays the FIRST output line and the fail-closed verdict is
    untouched by the (best-effort) detail query,
  * an old marker reads as stale and a fresh one does not, at the default 4h
    threshold and at an explicit override,
  * an unmeasurable age never reads as stale, and
  * a failed detail query degrades to "no age" instead of a wrong age.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path


def _load():
    for parent in Path(__file__).resolve().parents:
        script_path = parent / ".github" / "scripts" / "release-freeze-check.py"
        if script_path.exists():
            break
    else:
        raise RuntimeError("Could not find .github/scripts/release-freeze-check.py")
    loader = SourceFileLoader("release_freeze_check", str(script_path))
    mod = module_from_spec(spec_from_loader("release_freeze_check", loader))
    loader.exec_module(mod)
    return mod


def _gh(rc: int, out: str, err: str):
    """Build an injectable run_gh callable returning (rc, out, err)."""

    def _run(repo: str):
        return rc, out, err

    return _run


def test_queries_the_branch_namespace(monkeypatch):
    mod = _load()
    commands: list[str] = []

    class _Result:
        returncode = 0
        stdout = "0"
        stderr = ""

    def _fake_run(args, **_kwargs):
        commands.append(" ".join(args))
        return _Result()

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)
    verdict, _msg = mod.query_release_freeze("o/r", 1, 1)
    assert verdict == "open"
    assert len(commands) == 1
    assert "matching-refs/heads/release-freeze" in commands[0]
    assert "matching-refs/tags/release-freeze" not in commands[0]


def test_frozen_when_branch_present():
    mod = _load()
    verdict, msg = mod.query_release_freeze("o/r", 5, 5, _gh(0, "1", ""))
    assert verdict == "frozen"
    assert "present" in msg


def test_open_when_no_branch():
    mod = _load()
    verdict, _msg = mod.query_release_freeze("o/r", 5, 5, _gh(0, "0", ""))
    assert verdict == "open"


def test_fails_closed_when_every_attempt_blips():
    mod = _load()
    sleeps = []
    real_sleep = time.sleep
    time.sleep = lambda s: sleeps.append(s)  # type: ignore[assignment]
    try:
        verdict, msg = mod.query_release_freeze("o/r", 5, 5, _gh(1, "", "lookup failed: 503"))
    finally:
        time.sleep = real_sleep
    assert verdict == "fail-closed"
    assert "after 5 attempts" in msg
    # Final attempt must not sleep (no trailing delay).
    assert len(sleeps) == 4
    assert sleeps == [5, 10, 15, 20]
    # Underlying gh error surfaced in the message.
    assert "lookup failed: 503" in msg


def test_transient_blip_then_success_is_open():
    mod = _load()
    attempts = iter([(1, "", "blip"), (0, "0", "")])
    verdict, _msg = mod.query_release_freeze("o/r", 5, 5, lambda repo: next(attempts))
    assert verdict == "open"


def test_non_numeric_output_fails_closed():
    mod = _load()
    verdict, _msg = mod.query_release_freeze("o/r", 3, 1, _gh(0, "garbage", ""))
    assert verdict == "fail-closed"


def test_missing_repo_fails_closed(monkeypatch):
    mod = _load()
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    rc = mod.main([])
    assert rc == 2


# ---------------------------------------------------------------------------
# FAR-2326: marker detail + stale flag
# ---------------------------------------------------------------------------


def test_parse_marker_splits_sha_and_stamp():
    mod = _load()
    assert mod.parse_marker("abc1234\t2026-11-01T00:00:00Z") == (
        "abc1234",
        "2026-11-01T00:00:00Z",
    )


def test_parse_marker_rejects_malformed_payloads():
    mod = _load()
    assert mod.parse_marker("abc1234") is None
    assert mod.parse_marker("") is None
    assert mod.parse_marker("\t2026-11-01T00:00:00Z") is None
    assert mod.parse_marker("abc1234\t") is None


def test_commit_age_seconds_measures_a_fresh_marker():
    mod = _load()
    now = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    assert mod.commit_age_seconds("2026-11-03T11:00:00Z", now) == 3600


def test_commit_age_seconds_measures_a_stranded_marker():
    mod = _load()
    now = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    # The 2026-11-02/03 incident shape: the marker outlived ~13h.
    assert mod.commit_age_seconds("2026-11-02T23:00:00Z", now) == 13 * 3600


def test_commit_age_seconds_is_unmeasurable_for_a_non_iso_stamp():
    mod = _load()
    assert mod.commit_age_seconds("not a timestamp") is None


def test_commit_age_seconds_clamps_a_future_stamp_to_zero():
    mod = _load()
    now = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    assert mod.commit_age_seconds("2026-11-04T12:00:00Z", now) == 0


def test_is_stale_boundary_is_inclusive_at_the_threshold():
    mod = _load()
    assert mod.is_stale(4 * 3600 - 1, 4) is False
    assert mod.is_stale(4 * 3600, 4) is True
    assert mod.is_stale(2 * 3600, 1) is True


def test_max_freeze_hours_defaults_to_four(monkeypatch):
    mod = _load()
    monkeypatch.delenv("FREEZE_MAX_HOURS", raising=False)
    assert mod.max_freeze_hours() == 4.0


def test_max_freeze_hours_honours_a_valid_override(monkeypatch):
    mod = _load()
    monkeypatch.setenv("FREEZE_MAX_HOURS", "1.5")
    assert mod.max_freeze_hours() == 1.5


def test_max_freeze_hours_rejects_a_non_numeric_override(monkeypatch, capsys):
    mod = _load()
    monkeypatch.setenv("FREEZE_MAX_HOURS", "as long as it takes")
    assert mod.max_freeze_hours() == 4.0
    assert "FREEZE_MAX_HOURS" in capsys.readouterr().err


def test_detail_lines_omits_the_age_when_the_marker_query_fails(capsys):
    mod = _load()
    assert not mod.detail_lines("o/r", 4.0, _gh(1, "", "boom"))
    assert "marker detail unavailable" in capsys.readouterr().err


def test_detail_lines_reports_only_the_sha_when_the_stamp_is_unparseable(capsys):
    mod = _load()
    lines = mod.detail_lines("o/r", 4.0, _gh(0, "abc1234\tnot-a-date", ""))
    assert lines == ["sha=abc1234"]
    assert "is not ISO-8601" in capsys.readouterr().err


def _run_main(mod, monkeypatch, capsys, verdict, marker):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(mod, "query_release_freeze", lambda *_args, **_kw: (verdict, f"{verdict} msg"))
    monkeypatch.setattr(mod, "query_freeze_marker", lambda *_args, **_kw: marker)
    rc = mod.main([])
    return rc, capsys.readouterr().out


def test_main_reports_a_fresh_freeze_as_not_stale(monkeypatch, capsys):
    mod = _load()
    monkeypatch.delenv("FREEZE_MAX_HOURS", raising=False)
    rc, out = _run_main(mod, monkeypatch, capsys, "frozen", ("abc1234", 3600))
    lines = out.splitlines()
    assert rc == 0
    # The verdict stays the FIRST line; detail lines follow it.
    assert lines[0] == "frozen|frozen msg"
    assert "sha=abc1234" in lines
    assert "stale=false" in lines


def test_main_reports_a_stranded_freeze_as_stale(monkeypatch, capsys):
    mod = _load()
    monkeypatch.delenv("FREEZE_MAX_HOURS", raising=False)
    rc, out = _run_main(mod, monkeypatch, capsys, "frozen", ("abc1234", 13 * 3600))
    lines = out.splitlines()
    assert rc == 0
    assert "age_seconds=46800" in lines
    assert "stale=true" in lines
    assert "max_freeze_hours=4" in lines


def test_main_applies_a_threshold_override(monkeypatch, capsys):
    mod = _load()
    monkeypatch.setenv("FREEZE_MAX_HOURS", "13")
    # The same 12h marker: stale at the default 4h, NOT stale at an explicit 13h.
    rc, out = _run_main(mod, monkeypatch, capsys, "frozen", ("abc1234", 12 * 3600))
    lines = out.splitlines()
    assert rc == 0
    assert "stale=false" in lines
    assert "max_freeze_hours=13" in lines


def test_main_omits_detail_lines_for_an_open_gate(monkeypatch, capsys):
    mod = _load()
    monkeypatch.setattr(mod, "query_freeze_marker", lambda *_a, **_k: None)
    rc, out = _run_main(mod, monkeypatch, capsys, "open", None)
    assert rc == 0
    assert out.splitlines() == ["open|open msg"]


def test_fail_closed_never_queries_the_marker(monkeypatch, capsys):
    mod = _load()
    queried: list[str] = []

    def _marker(*args, **_kwargs):
        queried.append(args[0] if args else "")
        return ("abc1234", 1)

    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(mod, "query_release_freeze", lambda *_a, **_k: ("fail-closed", "api down"))
    monkeypatch.setattr(mod, "query_freeze_marker", _marker)
    rc = mod.main([])
    assert rc == 2
    # A fail-closed verdict must be a single line and must not add an API call.
    assert not queried
    assert capsys.readouterr().out.splitlines() == ["fail-closed|api down"]
