"""Linux-testable contracts for the native launcher's platform seam."""

from __future__ import annotations

from pathlib import Path

import pytest

import modulo.launcher.platform as platform_module


def test_linux_data_dir_preserves_xdg_contract(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(platform_module.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    assert platform_module.default_data_dir() == tmp_path / "xdg" / "modulo" / "data"


def test_windows_data_dir_uses_programdata_without_importing_windows_modules(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(platform_module.sys, "platform", "win32")
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))

    assert platform_module.default_data_dir() == tmp_path / "Modulo" / "data"
    assert platform_module.binary_name("postgres") == "postgres.exe"
    assert platform_module.initdb_locale() == "C"
    assert platform_module.supports_posix_file_locking() is False
    assert platform_module.supports_posix_process_groups() is False
    assert platform_module.supports_posix_security_bits() is False


def test_windows_data_dir_fails_closed_when_programdata_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_module.sys, "platform", "win32")
    monkeypatch.delenv("PROGRAMDATA", raising=False)

    with pytest.raises(platform_module.LauncherPlatformError, match="PROGRAMDATA"):
        platform_module.default_data_dir()


def test_posix_locale_and_binary_name_remain_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_module.sys, "platform", "linux")

    assert platform_module.binary_name("postgres") == "postgres"
    assert platform_module.initdb_locale() == "C.UTF-8"
    assert platform_module.supports_posix_process_groups() is True
    assert platform_module.supports_posix_file_locking() is True


def test_posix_security_bits_require_both_a_posix_os_and_a_non_windows_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chmod seam is only meaningful on a POSIX OS that is not Windows.

    Both operands are driven through the seam (never the host) so the pair is
    covered on Linux CI and on a Windows developer host alike.
    """
    monkeypatch.setattr(platform_module.os, "name", "posix")
    monkeypatch.setattr(platform_module.sys, "platform", "linux")
    assert platform_module.supports_posix_security_bits() is True

    monkeypatch.setattr(platform_module.sys, "platform", "win32")
    assert platform_module.supports_posix_security_bits() is False
