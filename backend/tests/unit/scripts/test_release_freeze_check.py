"""Unit tests for .github/scripts/release-freeze-check.py.

These prove the shared release-freeze gate used by both the merge-queue
`freeze-gate` job and the pre-merge TOCTOU re-verify step:
  * detects an active freeze (tag present) on the first successful response,
  * treats a missing tag as open (gate clear),
  * retries transient gh failures/blips and only FAILS CLOSED once every attempt
    is exhausted (so a single 503 cannot abort the tick or lift the freeze),
  * skips the backoff sleep on the final attempt (no pointless trailing delay),
  * surfaces the captured gh stderr in the fail-closed message so a persistent
    outage is diagnosable, and
  * FAILS CLOSED on any unverifiable response (never fails open).
"""

from __future__ import annotations

import time
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


def test_frozen_when_tag_present():
    mod = _load()
    verdict, msg = mod.query_release_freeze("o/r", 5, 5, _gh(0, "1", ""))
    assert verdict == "frozen"
    assert "present" in msg


def test_open_when_no_tag():
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
