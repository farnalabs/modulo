"""Platform-seam lock for the launcher secrets file (FAR-671 / FAR-2229).

The functional logic tests bypass the platform guard (mirroring the initdb
tests); this module installs no bypass, so the REAL guard is exercised — the
0600/ACL contract is POSIX-only and Windows must fail loudly instead of
silently writing launcher secrets with default ACLs.

The Windows refusal is driven through the module's own platform seam rather
than a ``sys.platform`` skip, so the refusal body executes on EVERY host
(CI runs Linux). A skip would have left the guard unexercised exactly where
the changed-lines coverage gate measures it.
"""

from pathlib import Path

import pytest

import modulo.launcher.secrets_file as secrets_file_module
from modulo.launcher.secrets_file import SecretsFileError, load_or_create


def test_assert_supported_platform_refuses_windows_until_acl_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(secrets_file_module, "is_windows", lambda: True)

    with pytest.raises(SecretsFileError, match="installer-host ACL validation"):
        secrets_file_module.assert_supported_platform()


def test_assert_supported_platform_allows_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secrets_file_module, "is_windows", lambda: False)

    assert secrets_file_module.assert_supported_platform() is None


def test_load_or_create_refuses_windows_before_writing_any_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal must happen BEFORE a credential file is created or opened."""
    monkeypatch.setattr(secrets_file_module, "is_windows", lambda: True)
    path = tmp_path / "secrets.json"

    with pytest.raises(SecretsFileError, match="installer-host ACL validation"):
        load_or_create(path)
    assert not path.exists()
