"""Pytest port of tools/tests/test-pre-commit-config.ps1 (FAR-300).

Verifies the pre-commit hooks are cross-platform Python/uv entries and are
never wrapped in Bash, and (FAR-2308) that every `uv ... run` invocation is
`--frozen` so no hook can rewrite backend/uv.lock as a side effect — in the
`entry:` lines, in the wrapper scripts those entries invoke, and in the gate's
own `files:`/ordering wiring.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

REPO_ROOT = str(Path(__file__).resolve().parents[3])


def _config() -> str:
    with Path(REPO_ROOT, ".pre-commit-config.yaml").open(encoding="utf-8") as fh:
        return fh.read()


def _hooks() -> list[dict]:
    """Every hook definition, in file order."""
    data = yaml.safe_load(_config())
    assert isinstance(data, dict), ".pre-commit-config.yaml is not a mapping"
    hooks: list[dict] = []
    for repo in data.get("repos") or []:
        assert isinstance(repo, dict), "a repos: entry is not a mapping"
        hooks.extend(repo.get("hooks") or [])
    assert hooks, ".pre-commit-config.yaml declares no hooks"
    return hooks


def _hook(hook_id: str) -> dict:
    matches = [hook for hook in _hooks() if hook.get("id") == hook_id]
    assert len(matches) == 1, f"expected exactly one hook with id {hook_id!r}, found {len(matches)}"
    return matches[0]


def _uv_run_segments(entry: str) -> list[str]:
    """The ``uv ... run`` invocations in one ``entry:`` line.

    A pre-push entry carries a second invocation after `` -- `` (the gated
    command), so both halves are inspected.
    """
    return [segment for segment in entry.split(" -- ") if re.search(r"(?:^|\s)uv(?:\s|$).*\brun\b", segment)]


def test_import_linter_runs_through_backend_project_environment():
    config = _config()
    assert re.search(r"(?m)^\s*entry:\s*uv --directory backend run --no-sync --frozen lint-imports\s*$", config)


def test_no_bash_wrapped_uv_hooks():
    config = _config()
    assert not re.search(r"(?m)^\s*entry:\s*(?:/bin/)?bash\b[^\r\n]*\buv\b", config)


def test_migration_collision_check_runs_through_cross_platform_python_script():
    config = _config()
    assert re.search(
        r"(?m)^\s*entry:\s*uv run --project backend --no-sync --frozen python "
        r"scripts/run_check_migration_heads\.py\s*$",
        config,
    )
    assert Path(REPO_ROOT, "scripts", "run_check_migration_heads.py").is_file()


def test_check_c1_chars_files_pattern_matches_workflow_files():
    """Regression guard (FAR-1350): a doubled-backslash pattern (``\\\\.github``)
    requires a literal backslash in the path, so the hook never fired. The
    pattern must stay single-backslash and match real workflow paths."""
    config = _config()
    lines = config.splitlines()
    id_indexes = [i for i, line in enumerate(lines) if line.strip() == "- id: check-c1-chars"]
    assert id_indexes, "check-c1-chars hook missing from .pre-commit-config.yaml"
    pattern_lines = [line for line in lines[id_indexes[0] + 1 :] if line.strip().startswith("files:")]
    assert pattern_lines, "check-c1-chars hook has no files: pattern"
    pattern = pattern_lines[0].split("files: ", 1)[1].strip()
    assert "\\\\" not in pattern
    compiled = re.compile(pattern)
    assert compiled.search(".github/workflows/ci.yml")
    assert compiled.search(".github/workflows/ci.yaml")
    assert compiled.search(".github/workflows/sub/deploy.yml")
    assert not compiled.search("backend/src/farna/api/routes/runs.py")


def test_every_uv_run_invocation_is_frozen():
    """FAR-2308: no hook may rewrite backend/uv.lock as a side effect.

    ``uv run`` / ``uv --directory backend run`` updates ``backend/uv.lock`` when
    it has drifted from ``backend/pyproject.toml``. During a commit gate that
    mutates the tree between pre-commit's stash and restore, which aborts the
    commit and every later push from a branch based on the stale tree.
    ``--frozen`` forbids the lockfile write; it does not change what the hook
    runs. Pre-push entries carry a second invocation after `` -- `` (the gated
    command), so both are checked.
    """
    config = _config()
    entry_lines = [line.strip() for line in config.splitlines() if line.strip().startswith("entry:")]
    offenders = [line for line in entry_lines if any("--frozen" not in segment for segment in _uv_run_segments(line))]
    assert not offenders, (
        "pre-commit hook uv invocations must pass `--frozen` (FAR-2308) so a stale "
        "backend/uv.lock is never rewritten mid-gate:\n" + "\n".join(offenders)
    )


def test_check_uv_lock_files_pattern_covers_every_lock_input():
    """FAR-2308: the gate fires on both lock inputs and on itself.

    ``backend/pyproject.toml`` (a dependency or version edit), ``backend/uv.lock``
    (a hand-edited or rebased lock) and the checker itself all invalidate the
    verdict, so each must trigger the hook. Unrelated backend sources must not:
    the check spawns a resolver and would otherwise run on every backend edit.
    """
    pattern = _hook("check-uv-lock").get("files")
    assert isinstance(pattern, str), "check-uv-lock has no files: pattern"
    compiled = re.compile(pattern)
    missing = [
        path
        for path in (
            "backend/pyproject.toml",
            "backend/uv.lock",
            "scripts/run_check_uv_lock.py",
        )
        if not compiled.search(path)
    ]
    unexpected = [
        path
        for path in (
            "backend/src/farna/api/routes/runs.py",
            "backend/tests/unit/scripts/test_run_check_uv_lock.py",
            "scripts/run_vulture.py",
            "frontend/package.json",
        )
        if compiled.search(path)
    ]
    assert not missing, "check-uv-lock's files: pattern never fires on these lock inputs:\n" + "\n".join(
        f"  {path}" for path in missing
    )
    assert not unexpected, "check-uv-lock's files: pattern fires on unrelated paths:\n" + "\n".join(
        f"  {path}" for path in unexpected
    )


def test_check_uv_lock_precedes_every_other_uv_run_hook():
    """FAR-2308: the lock gate runs before any hook that could rewrite the lock.

    ``--frozen`` is the primary defence (test_every_uv_run_invocation_is_frozen).
    Ordering is the second: a later hook that lost ``--frozen`` still runs only
    after the lock gate has already failed the run, so it cannot rewrite the
    lock on a commit the gate would reject. Only hooks sharing a stage with
    check-uv-lock can run in the same pass, so only they are compared.
    """
    hooks = _hooks()
    ids = [hook.get("id") for hook in hooks]
    assert "check-uv-lock" in ids, "check-uv-lock hook is missing"
    lock_index = ids.index("check-uv-lock")
    lock_stages = set(_hook("check-uv-lock").get("stages") or ["pre-commit"])
    compared = [
        hook_id
        for hook_id in ids
        if hook_id != "check-uv-lock"
        and lock_stages & set(_hook(hook_id).get("stages") or ["pre-commit"])
        and _uv_run_segments(str(_hook(hook_id).get("entry", "")))
    ]
    assert compared, "no other uv-run hook shares check-uv-lock's stage — the ordering guard went vacuous"
    offenders = [hook_id for hook_id in compared if ids.index(hook_id) < lock_index]
    assert not offenders, (
        "check-uv-lock must be declared before every other uv-run hook in the same stage "
        "(FAR-2308), so a hook that could rewrite backend/uv.lock cannot run first:\n"
        + "\n".join(
            f"  {hook_id} (index {ids.index(hook_id)}) precedes check-uv-lock (index {lock_index})"
            for hook_id in offenders
        )
    )


def _uv_run_argv(node: ast.AST) -> list[str] | None:
    """The literal string argv of an ``ast.List`` that starts with ``uv ... run``."""
    if not isinstance(node, ast.List):
        return None
    elements: list[str] = []
    for element in node.elts:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            return None
        elements.append(element.value)
    if len(elements) < 2 or elements[0] != "uv":
        return None
    if "run" not in elements:
        return None
    return elements


def test_hook_wrapper_uv_subprocess_argv_is_frozen():
    """FAR-2308: a hook wrapper's own ``uv run`` argv must be ``--frozen`` too.

    The ``entry:`` lines are only half the surface: several wrappers re-invoke
    ``uv`` as a subprocess (semgrep, vulture, the changed-test runner, the
    migration-heads check). Without ``--frozen`` any of them rewrites
    ``backend/uv.lock`` mid-gate exactly like an unwrapped entry line would.
    """
    entry_text = "\n".join(str(hook.get("entry", "")) for hook in _hooks())
    script_names = sorted(set(re.findall(r"scripts/([A-Za-z0-9_]+\.py)", entry_text)))
    assert "run_check_uv_lock.py" in script_names, "hook entries no longer reference the uv-lock checker"
    offenders: list[str] = []
    for name in script_names:
        path = Path(REPO_ROOT, "scripts", name)
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            argv = _uv_run_argv(node)
            if argv is not None and "--frozen" not in argv:
                offenders.append(f"  scripts/{name}: {' '.join(argv)}")
    assert not offenders, (
        "hook wrapper scripts must pass `--frozen` on every uv-run subprocess argv "
        "(FAR-2308) so a stale backend/uv.lock is never rewritten mid-gate:\n" + "\n".join(offenders)
    )
