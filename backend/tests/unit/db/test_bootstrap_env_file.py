"""Direct tests for the promoted bootstrap env-file writer (FAR-671).

``modulo.db.bootstrap._write_env_file`` is the exclusive-create +
symlink-rejecting 0600 writer used by the container entrypoint AND (since
the promotion) by the native launcher's pinned config file
(``launcher.config_source.write_pinned_env_file``). These tests lock the
security-critical behaviour directly: correct content, 0600 mode on POSIX,
atomic replacement of an existing file, random-sibling resilience against
temp-name collisions, and the symlink-squat refusal that never follows or
truncates the victim.
"""

import sys
from pathlib import Path

import pytest

from modulo.db.bootstrap import _write_env_file

requires_posix = pytest.mark.skipif(sys.platform != "linux", reason="POSIX mode/symlink semantics")


def test_write_env_file_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "database_url.env"
    _write_env_file(str(target), "postgresql://modulo:pw@localhost:5432/modulo")
    assert target.read_text(encoding="utf-8") == "postgresql://modulo:pw@localhost:5432/modulo"


@requires_posix
def test_write_env_file_mode_is_0600(tmp_path: Path) -> None:
    target = tmp_path / "database_url.env"
    _write_env_file(str(target), "content")
    mode = target.stat().st_mode & 0o777
    assert mode == 0o600


def test_write_env_file_replaces_existing_content(tmp_path: Path) -> None:
    target = tmp_path / "database_url.env"
    target.write_text("stale-credentials")
    _write_env_file(str(target), "fresh-credentials")
    assert target.read_text(encoding="utf-8") == "fresh-credentials"


def test_write_env_file_survives_tmp_name_collision(tmp_path: Path) -> None:
    """A pre-existing file matching the random-sibling pattern cannot win."""
    target = tmp_path / "database_url.env"
    (tmp_path / "database_url.env.tmp-squatter").write_text("squatted")
    _write_env_file(str(target), "real-content")
    assert target.read_text(encoding="utf-8") == "real-content"


@requires_posix
def test_write_env_file_refuses_symlink_squat(tmp_path: Path) -> None:
    """A symlink on the contract path is rejected; the victim is untouched."""
    victim = tmp_path / "victim.txt"
    victim.write_text("innocent")
    target = tmp_path / "database_url.env"
    target.symlink_to(victim)
    with pytest.raises(RuntimeError, match="symlink"):
        _write_env_file(str(target), "malicious-content")
    assert victim.read_text(encoding="utf-8") == "innocent"
    assert target.is_symlink()


@requires_posix
def test_write_env_file_leaves_no_tmp_debris(tmp_path: Path) -> None:
    target = tmp_path / "database_url.env"
    _write_env_file(str(target), "content")
    siblings = [entry.name for entry in tmp_path.iterdir() if entry.name != target.name]
    assert not siblings
