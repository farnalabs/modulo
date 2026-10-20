"""Focused coverage for the S3776 decomposition of the ``logs`` CLI command.

``_echo_log_contents`` / ``_follow_log`` were extracted from ``logs``; these
cases exercise the follow loop (new bytes and idle ticks), the Ctrl-C exit and
the read-failure exit that the tabular command tests never reach.
"""

from pathlib import Path

import pytest

from modulo.cli.main import _echo_log_contents, _follow_log


def test_echo_log_contents_with_follow_tails_new_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_path = tmp_path / "launcher.log"
    log_path.write_text("first line\n", encoding="utf-8")
    calls = 0

    def _sleep(_seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write("second line\n")
            return
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", _sleep)

    _echo_log_contents(log_path, follow=True)

    output = capsys.readouterr().out
    assert "first line" in output
    assert "second line" in output
    assert "following" in output


def test_follow_log_returns_on_keyboard_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_path = tmp_path / "launcher.log"
    log_path.write_text("", encoding="utf-8")

    def _interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", _interrupt)

    _follow_log(log_path)


def test_follow_log_exits_one_on_read_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_path = tmp_path / "launcher.log"
    log_path.write_text("", encoding="utf-8")

    def _fail(_seconds: float) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr("time.sleep", _fail)

    with pytest.raises(SystemExit) as exc:
        _follow_log(log_path)

    assert exc.value.code == 1
    assert "log write/read failed" in capsys.readouterr().out


def test_follow_log_idle_tick_then_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_path = tmp_path / "launcher.log"
    log_path.write_text("", encoding="utf-8")
    calls = 0

    def _sleep(_seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", _sleep)

    _follow_log(log_path)
