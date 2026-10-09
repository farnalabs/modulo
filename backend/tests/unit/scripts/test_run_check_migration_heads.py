"""Unit tests for scripts/run_check_migration_heads.py (Alembic collision gate).

The gate only evaluates migration files staged/changed in the current commit, so
these tests drive ``_main`` against a throwaway ``versions/`` tree (both the
module-level ``VERSIONS_DIR`` and the resolved repo root are pointed at it) and
substitute ``subprocess.run`` for the trailing non-fatal ``alembic heads`` probe.

They pin the three collision arms (duplicate numeric prefix, duplicate revision
id, duplicate down_revision), the changed-only scoping that leaves pre-existing
collisions alone, the intentional-fork exemption (a revision that a merge
migration lists as a parent), the ``--diff-range`` validation and empty-diff
early return, and both non-fatal alembic-probe arms.
"""

from __future__ import annotations

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest

for parent in Path(__file__).resolve().parents:
    script_path = parent / "scripts" / "run_check_migration_heads.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find repo root (scripts/run_check_migration_heads.py)")

_loader = SourceFileLoader("run_check_migration_heads", str(script_path))
mod = module_from_spec(spec_from_loader("run_check_migration_heads", _loader))
_loader.exec_module(mod)


class _Completed:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _versions_dir(tmp_path: Path) -> Path:
    versions = tmp_path / "backend" / "src" / "modulo" / "db" / "migrations" / "versions"
    versions.mkdir(parents=True)
    return versions


def _write_migration(versions: Path, name: str, revision: str, down_revision: str | None) -> None:
    body = f'revision: str = "{revision}"\n'
    if down_revision is None:
        body += "down_revision: str | None = None\n"
    else:
        body += f'down_revision: str | None = "{down_revision}"\n'
    (versions / name).write_text(body, encoding="utf-8")


def _write_merge_migration(versions: Path, name: str, revision: str, parents: tuple[str, ...]) -> None:
    quoted = "\n".join(f'    "{parent}",' for parent in parents)
    body = f'revision: str = "{revision}"\ndown_revision: str | Sequence[str] | None = (\n{quoted}\n)\n'
    (versions / name).write_text(body, encoding="utf-8")


def _prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    changed: list[str],
    alembic_stdout: str = "<base> (head)\n",
    alembic_raises: bool = False,
) -> Path:
    """Point the gate at a throwaway tree and stub the changed-set + alembic probe."""
    versions = _versions_dir(tmp_path)
    monkeypatch.setattr(mod, "VERSIONS_DIR", str(versions))
    monkeypatch.setattr(mod, "_resolve_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(mod, "_changed_names", lambda _diff_range=None: list(changed))
    if alembic_raises:

        def _boom(*_args, **_kwargs):
            raise OSError("uv not found")

        monkeypatch.setattr(mod.subprocess, "run", _boom)
    else:
        monkeypatch.setattr(mod.subprocess, "run", lambda *_args, **_kwargs: _Completed(0, stdout=alembic_stdout))
    return versions


class TestRead:
    def test_read_returns_file_contents(self, tmp_path: Path) -> None:
        target = tmp_path / "0062_add_widget.py"
        target.write_text('revision: str = "0062_add_widget"\n', encoding="utf-8")
        assert "0062_add_widget" in mod._read(str(target))

    def test_read_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert not mod._read(str(tmp_path / "does_not_exist.py"))


class TestChangedNames:
    def test_staged_diff_filters_to_migration_versions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stdout = (
            "backend/src/modulo/db/migrations/versions/0062_add_widget.py\n"
            "backend/src/modulo/api/routes/triggers.py\n"
            "backend/src/modulo/db/migrations/versions/0063_fix.py\n"
            "README.md\n"
        )
        calls: list[list[str]] = []

        def _fake_run(cmd, **_kwargs):
            calls.append(list(cmd))
            return _Completed(0, stdout=stdout)

        monkeypatch.setattr(mod.subprocess, "run", _fake_run)

        assert mod._changed_names(None) == ["0062_add_widget.py", "0063_fix.py"]
        assert calls[0] == ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"]

    def test_diff_range_forwards_the_range(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []

        def _fake_run(cmd, **_kwargs):
            calls.append(list(cmd))
            return _Completed(0, stdout="backend/src/modulo/db/migrations/versions/0062_add_widget.py\n")

        monkeypatch.setattr(mod.subprocess, "run", _fake_run)

        assert mod._changed_names("main...HEAD") == ["0062_add_widget.py"]
        assert calls[0] == ["git", "diff", "--name-only", "--diff-filter=ACMR", "main...HEAD"]

    def test_invalid_diff_range_exits_without_shelling_out(self, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        def _forbidden_run(*_args, **_kwargs):
            raise AssertionError("git must not run for an invalid --diff-range")

        monkeypatch.setattr(mod.subprocess, "run", _forbidden_run)

        with pytest.raises(SystemExit) as excinfo:
            mod._changed_names("main...HEAD; rm -rf /")

        assert excinfo.value.code == 1
        assert "invalid --diff-range" in capsys.readouterr().err


class TestResolveRepoRoot:
    def test_prefers_git_toplevel_holding_versions_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _versions_dir(tmp_path)
        monkeypatch.setattr(mod.subprocess, "run", lambda *_a, **_k: _Completed(0, stdout=f"{tmp_path}\n"))
        assert mod._resolve_repo_root() == str(tmp_path)

    def test_falls_back_when_toplevel_lacks_versions_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bare = tmp_path / "bare"
        bare.mkdir()
        monkeypatch.setattr(mod.subprocess, "run", lambda *_a, **_k: _Completed(0, stdout=f"{bare}\n"))
        monkeypatch.setattr(mod, "REPO_ROOT", "/fallback/root")
        assert mod._resolve_repo_root() == "/fallback/root"

    def test_falls_back_on_git_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mod.subprocess, "run", lambda *_a, **_k: _Completed(128, stderr="fatal: not a repo\n"))
        monkeypatch.setattr(mod, "REPO_ROOT", "/fallback/root")
        assert mod._resolve_repo_root() == "/fallback/root"


class TestCollectMergeParentRevisions:
    def test_reads_revisions_and_tuple_form_parents(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        versions = _versions_dir(tmp_path)
        monkeypatch.setattr(mod, "VERSIONS_DIR", str(versions))
        _write_migration(versions, "0001_base.py", "0001_base", None)
        _write_merge_migration(versions, "0002_merge.py", "0002_merge", ("0001_base", "0000_other"))

        parents, revisions = mod._collect_merge_parent_revisions(["0001_base.py", "0002_merge.py"])

        assert parents == {"0001_base", "0000_other"}
        assert revisions == {"0001_base.py": "0001_base", "0002_merge.py": "0002_merge"}

    def test_string_down_revision_is_not_a_merge_parent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        versions = _versions_dir(tmp_path)
        monkeypatch.setattr(mod, "VERSIONS_DIR", str(versions))
        _write_migration(versions, "0002_child.py", "0002_child", "0001_base")

        parents, revisions = mod._collect_merge_parent_revisions(["0002_child.py"])

        assert not parents
        assert revisions == {"0002_child.py": "0002_child"}

    def test_file_without_revision_is_skipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        versions = _versions_dir(tmp_path)
        monkeypatch.setattr(mod, "VERSIONS_DIR", str(versions))
        (versions / "broken.py").write_text("x = 1\n", encoding="utf-8")

        parents, revisions = mod._collect_merge_parent_revisions(["broken.py"])

        assert not parents
        assert not revisions


class TestMainSkips:
    def test_missing_versions_dir_returns_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        monkeypatch.setattr(mod, "_resolve_repo_root", lambda: str(tmp_path))
        assert mod._main([]) == 0
        assert "versions dir not found" in capsys.readouterr().err


class TestMainClean:
    def test_clean_tree_passes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        versions = _prepare(tmp_path, monkeypatch, changed=["0062_add_widget.py"])
        _write_migration(versions, "0062_add_widget.py", "0062_add_widget", "0061_prev")

        assert mod._main([]) == 0
        assert "no migration number/revision collisions" in capsys.readouterr().err


class TestMainCollisions:
    def test_duplicate_prefix_involving_changed_file_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        versions = _prepare(tmp_path, monkeypatch, changed=["0062_add_widget.py"])
        _write_migration(versions, "0062_add_widget.py", "0062_a", "0061_x")
        _write_migration(versions, "0062_add_gadget.py", "0062_b", "0061_y")

        assert mod._main([]) == 1
        err = capsys.readouterr().err
        assert "duplicate migration number '0062'" in err
        assert "FAILED" in err

    def test_preexisting_collision_outside_changed_set_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        versions = _prepare(tmp_path, monkeypatch, changed=["0099_unrelated.py"])
        _write_migration(versions, "0062_add_widget.py", "0062_a", "0061_x")
        _write_migration(versions, "0062_add_gadget.py", "0062_b", "0061_y")

        assert mod._main(["--diff-range", "main...HEAD"]) == 0
        assert "no migration number/revision collisions" in capsys.readouterr().err

    def test_duplicate_revision_id_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        versions = _prepare(tmp_path, monkeypatch, changed=["0062_a.py"])
        _write_migration(versions, "0062_a.py", "shared_rev", "0061_x")
        _write_migration(versions, "0063_b.py", "shared_rev", "0061_y")

        assert mod._main([]) == 1
        assert "duplicate revision id 'shared_rev'" in capsys.readouterr().err

    def test_duplicate_down_revision_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        versions = _prepare(tmp_path, monkeypatch, changed=["0062_a.py"])
        _write_migration(versions, "0062_a.py", "rev_a", "shared_parent")
        _write_migration(versions, "0063_b.py", "rev_b", "shared_parent")

        assert mod._main([]) == 1
        err = capsys.readouterr().err
        assert "both declare down_revision 'shared_parent'" in err
        assert "unintended branch" in err

    def test_falls_back_to_all_files_when_nothing_staged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        versions = _prepare(tmp_path, monkeypatch, changed=[])
        _write_migration(versions, "0062_a.py", "rev_a", "0061_x")
        _write_migration(versions, "0062_b.py", "rev_b", "0061_y")

        assert mod._main([]) == 1
        assert "duplicate migration number '0062'" in capsys.readouterr().err

    def test_intentional_fork_revision_is_exempt(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        versions = _prepare(tmp_path, monkeypatch, changed=["0062_a.py"])
        _write_migration(versions, "0062_a.py", "forked_rev", "0061_x")
        _write_migration(versions, "0063_b.py", "forked_rev", "0061_y")
        _write_merge_migration(versions, "0064_merge.py", "merge_rev", ("forked_rev", "other_rev"))

        assert mod._main([]) == 0
        assert "no migration number/revision collisions" in capsys.readouterr().err


class TestMainDiffRange:
    def test_empty_diff_range_returns_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        versions = _prepare(tmp_path, monkeypatch, changed=[])
        _write_migration(versions, "0062_a.py", "rev_a", "0061_x")

        assert mod._main(["--diff-range", "main...HEAD"]) == 0
        assert "no migration files changed" in capsys.readouterr().err


class TestMainAlembicProbe:
    def test_multiple_heads_warns_but_still_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        versions = _prepare(
            tmp_path,
            monkeypatch,
            changed=["0062_a.py"],
            alembic_stdout="abc123 (head)\ndef456 (head)\n",
        )
        _write_migration(versions, "0062_a.py", "rev_a", "0061_x")

        assert mod._main([]) == 0
        err = capsys.readouterr().err
        assert "WARNING" in err
        assert "2 migration heads" in err
        assert "no migration number/revision collisions" in err

    def test_single_head_does_not_warn(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        versions = _prepare(tmp_path, monkeypatch, changed=["0062_a.py"], alembic_stdout="abc123 (head)\n")
        _write_migration(versions, "0062_a.py", "rev_a", "0061_x")

        assert mod._main([]) == 0
        assert "WARNING" not in capsys.readouterr().err

    def test_alembic_failure_is_non_fatal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        versions = _prepare(tmp_path, monkeypatch, changed=["0062_a.py"], alembic_raises=True)
        _write_migration(versions, "0062_a.py", "rev_a", "0061_x")

        assert mod._main([]) == 0
        assert "could not run 'alembic heads'" in capsys.readouterr().err
