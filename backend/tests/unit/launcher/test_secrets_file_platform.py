"""Windows-refusal lock for the launcher secrets file (FAR-671).

The functional logic tests bypass the platform guard (mirroring the initdb
tests); this module installs no bypass, so the REAL guard is exercised —
the 0600/ACL contract is POSIX-only and Windows must fail loudly instead of
silently writing launcher secrets with default ACLs.
"""

import sys
from pathlib import Path

import pytest

from modulo.launcher.secrets_file import SecretsFileError, load_or_create


@pytest.mark.skipif(sys.platform != "win32", reason="the refusal seam is Windows-specific")
def test_load_or_create_refuses_windows_today(tmp_path: Path) -> None:
    with pytest.raises(SecretsFileError, match=r"TODO\(P3\)"):
        load_or_create(tmp_path / "secrets.json")
