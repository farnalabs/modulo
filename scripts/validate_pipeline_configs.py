"""Validate Modulo pipeline configuration patterns.

Checks for known bug patterns in sandbox_agent pipeline configs:
1. Empty ``agent_prompt`` that would cause opencode to hang (llm mode)
2. ``timeout_seconds`` defaulting to 600 (too short for complex tasks)
3. ``template_id`` not in the known-good set {opencode, modulo-opencode}
4. FAR-296 mode config: ``mode="script"`` requires a non-empty
   ``script_command``, and agent_command/agent_commands and script_command are
   mutually exclusive (mirrors the shared mode-aware validator used by the
   node runner, Pydantic model, and GraphValidator).
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any

import yaml

_EXIT_CODE = 0

_DEFAULT_FALLBACKS = [
    (
        "timeout_seconds",
        600,
        (
            "sandbox_agent timeout_seconds defaults to 600 — "
            "too short for complex tasks like rebase + lint fix + push. Use 1200 (20 min)."
        ),
    ),
    (
        "template_id",
        "base",
        (
            "sandbox_agent template_id defaults to 'base' — use 'opencode' (default, has opencode CLI) "
            "or 'modulo-opencode' (managed cache-warmed template)."
        ),
    ),
]

_ALLOWED_TEMPLATE_IDS = {"opencode", "modulo-opencode"}


def _fail(message: str) -> None:
    global _EXIT_CODE
    print(f"FAIL: {message}", file=sys.stderr)
    _EXIT_CODE = 1


def _string_get_call_with_constant_default(node: ast.AST) -> tuple[str, ast.Constant] | None:
    """Return ``(key, default_constant)`` when *node* is a ``.get("key", const)`` call, else ``None``."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "get":
        return None
    args = node.args
    if not args or not isinstance(args[0], ast.Constant) or not isinstance(args[0].value, str):
        return None
    default = args[1] if len(args) >= 2 else None
    if not isinstance(default, ast.Constant):
        return None
    return (args[0].value, default)


def _flag_matching_fallbacks(path: Path, node: ast.Call, key: str, default: ast.Constant) -> None:
    """Fail on any ``_DEFAULT_FALLBACKS`` entry matching this ``.get()`` call."""
    for fallback_key, bad_value, message in _DEFAULT_FALLBACKS:
        if key == fallback_key and default.value == bad_value:
            _fail(f"{path}:{node.lineno}: {message}")


def _scan_node_runner(path: Path) -> None:
    """AST-scan the node_runner for default fallback patterns."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        match = _string_get_call_with_constant_default(node)
        if match is None:
            continue
        key, default = match
        _flag_matching_fallbacks(path, node, key, default)


def _pipeline_config_paths() -> list[Path]:
    """Collect the sorted candidate pipeline config file paths under the cwd."""
    patterns = [
        "**/pipelines/*.json",
        "**/pipelines/*.yaml",
        "**/pipelines/*.yml",
        "**/pipeline*config*.json",
    ]
    found_files: set[Path] = set()
    for pattern in patterns:
        found_files.update(Path.cwd().glob(pattern))
    return sorted(found_files)


def _load_config_document(path: Path) -> dict[str, Any] | list[Any] | None:
    """Read and parse a JSON/YAML pipeline config, returning ``None`` when unparseable."""
    content = path.read_text(encoding="utf-8")
    try:
        if path.suffix == ".json":
            return json.loads(content)
        if path.suffix in (".yaml", ".yml"):
            return yaml.safe_load(content)
    except (ValueError, yaml.YAMLError):
        pass
    return None


def _config_objects(data: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    """Normalise a parsed config document into the object nodes to inspect."""
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return list(data)
    return []


def _scan_pipeline_config_files() -> None:
    """Scan JSON/YAML pipeline definitions for misconfigured sandbox_agent nodes."""
    for path in _pipeline_config_paths():
        data = _load_config_document(path)
        if not data:
            continue
        for obj in _config_objects(data):
            _check_node_config(path, obj)


def _check_sandbox_mode(node: dict[str, Any], path: Path, prefix: str) -> None:
    """FAR-296 mode-scoped checks: llm vs script command exclusivity.

    Mirrors the rules of the shared ``_validate_sandbox_mode_config`` helper in
    node_runner.py (this script is standalone and cannot import backend code).
    llm mode (default) keeps the existing agent_prompt check; script mode
    requires a non-empty ``script_command``; the two command families are
    mutually exclusive.
    """
    mode = node.get("mode", "llm")
    has_agent_command = bool(node.get("agent_command") or node.get("agent_commands"))
    has_script_command = bool(node.get("script_command"))
    if has_agent_command and has_script_command:
        _fail(
            f"{path}: sandbox_agent node {prefix} sets BOTH agent_command/agent_commands "
            "and script_command — the two modes are mutually exclusive"
        )
    if mode == "script":
        if not has_script_command:
            _fail(f"{path}: script mode sandbox_agent node {prefix} requires a non-empty 'script_command'")
        return
    ap = node.get("agent_prompt", "")
    if not ap:
        _fail(f"{path}: agent_prompt is empty in sandbox_agent node {prefix} — opencode will hang")


def _is_sandbox_node(node_type: str) -> bool:
    """Return ``True`` when a node type string identifies a sandbox_agent node."""
    return "sandbox_agent" in node_type or "sandbox" in node_type


def _check_sandbox_template(node: dict[str, Any], path: Path, prefix: str) -> None:
    """Check template_id is one of the known-good sandbox templates."""
    tid = node.get("template_id", "")
    if tid and tid not in _ALLOWED_TEMPLATE_IDS:
        _fail(
            f"{path}: template_id is '{tid}' in sandbox_agent node {prefix} — "
            "should be one of: opencode (default, has opencode CLI), "
            "modulo-opencode (managed cache-warmed template)"
        )


def _check_sandbox_timeout(node: dict[str, Any], path: Path, prefix: str) -> None:
    """Check timeout_seconds is not the default 600."""
    to = node.get("timeout_seconds", 600)
    if to == 600:
        _fail(
            f"{path}: timeout_seconds is {to} (default) in sandbox_agent node {prefix} — "
            "increase to 1200 for complex tasks"
        )


def _check_node_config(path: Path, obj: dict[str, Any], prefix: str = "") -> None:
    """Recursively inspect a pipeline dict for sandbox_agent node config issues."""
    node_type = obj.get("node_type") or obj.get("type") or ""
    if _is_sandbox_node(node_type):
        # Check mode-scoped config (FAR-296): script_command required in script
        # mode, commands mutually exclusive, agent_prompt required in llm mode.
        _check_sandbox_mode(obj, path, prefix)
        _check_sandbox_template(obj, path, prefix)
        _check_sandbox_timeout(obj, path, prefix)

    _check_node_config_children(path, obj, prefix)


def _check_node_config_children(path: Path, obj: dict[str, Any], prefix: str) -> None:
    """Recurse :func:`_check_node_config` into the node's children/edges collections."""
    for key in ("nodes", "edges", "items", "steps", "children"):
        children = obj.get(key)
        if not isinstance(children, list):
            continue
        for i, child in enumerate(children):
            if isinstance(child, dict):
                _check_node_config(path, child, f"{prefix}[{i}]")


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent

    # Scan source files
    node_runner = repo_root / "backend" / "src" / "modulo" / "core" / "pipeline_engine" / "node_runner.py"
    if node_runner.exists():
        _scan_node_runner(node_runner)

    # Scan pipeline config files
    _scan_pipeline_config_files()

    if _EXIT_CODE:
        print("\nSome pipeline config validations failed — see messages above.", file=sys.stderr)
    else:
        print("All pipeline config checks passed.")

    return _EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
