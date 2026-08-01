"""Validate Modulo pipeline configuration patterns.

Checks for known bug patterns in sandbox_agent pipeline configs:
1. Empty ``agent_prompt`` that would cause opencode to hang
2. ``timeout_seconds`` defaulting to 600 (too short for complex tasks)
3. ``template_id`` not set to ``"opencode"``
4. ``**env_vars_extra`` placed after system env vars (security bypass)
"""

from __future__ import annotations

import ast
import glob
import json
import sys
from pathlib import Path

_EXIT_CODE = 0


def _fail(message: str) -> None:
    global _EXIT_CODE
    print(f"FAIL: {message}", file=sys.stderr)
    _EXIT_CODE = 1


def _scan_node_runner(path: Path) -> None:
    """AST-scan the node_runner for default fallback patterns."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    for node in ast.walk(tree):
        # Look for `node_def.get("timeout_seconds", 600)` — default of 600
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "get":
                args = node.args
                if len(args) >= 1 and isinstance(args[0], ast.Constant) and args[0].value == "timeout_seconds":
                    default = args[1] if len(args) >= 2 else None
                    if default is not None and isinstance(default, ast.Constant) and default.value == 600:
                        _fail(
                            f"{path}:{node.lineno}: "
                            "sandbox_agent timeout_seconds defaults to 600 — "
                            "too short for complex tasks like rebase + lint fix + push. "
                            "Use 1200 (20 min)."
                        )

        # Look for template_id default to "base" — should be "opencode"
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "get":
                args = node.args
                if len(args) >= 1 and isinstance(args[0], ast.Constant) and args[0].value == "template_id":
                    default = args[1] if len(args) >= 2 else None
                    if default is not None and isinstance(default, ast.Constant) and default.value == "base":
                        _fail(
                            f"{path}:{node.lineno}: "
                            "sandbox_agent template_id defaults to 'base' — "
                            "use 'opencode' template (has opencode CLI pre-installed)."
                        )

    # Line-based checks for patterns that are hard to catch with AST
    for lineno, line in enumerate(source.splitlines(), start=1):
        # `**env_vars_extra` at end of envs dict (after system vars)
        stripped = line.strip()
        if "**env_vars_extra" in stripped and "}" not in stripped:
            _fail(
                f"{path}:{lineno}: "
                "`**env_vars_extra` must precede system env vars "
                "(GITHUB_TOKEN, APP_MODULO_OPENCODE_API_KEY) — "
                "otherwise pipeline config can override auth tokens."
            )


def _scan_mcp_server(path: Path) -> None:
    """AST-scan the MCP server for empty agent_prompt patterns."""
    source = path.read_text(encoding="utf-8")
    for lineno, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if 'n.get("agent_prompt", "")' in stripped or 'n.get("agent_prompt", "") or ""' in stripped:
            _fail(
                f"{path}:{lineno}: "
                "Empty agent_prompt fallback in MCP handler — "
                "opencode will hang with no instructions. "
                "Require a non-empty prompt template."
            )


def _scan_pipeline_config_files() -> None:
    """Scan JSON/YAML pipeline definitions for misconfigured sandbox_agent nodes."""
    patterns = [
        "**/pipelines/*.json",
        "**/pipelines/*.yaml",
        "**/pipelines/*.yml",
        "**/pipeline*config*.json",
    ]
    found_files = set()
    for pattern in patterns:
        found_files.update(glob.glob(pattern, recursive=True))

    for path_str in sorted(found_files):
        path = Path(path_str)
        content = path.read_text(encoding="utf-8")
        data: dict | list | None = None
        try:
            if path.suffix == ".json":
                data = json.loads(content)
            elif path.suffix in (".yaml", ".yml"):
                import yaml

                data = yaml.safe_load(content)
        except (json.JSONDecodeError, ValueError, ImportError):
            continue

        if not data:
            continue

        objs: list[dict] = [data] if isinstance(data, dict) else data if isinstance(data, list) else []
        for obj in objs:
            _check_node_config(path, obj)


def _check_node_config(path: Path, obj: dict, prefix: str = "") -> None:
    """Recursively inspect a pipeline dict for sandbox_agent node config issues."""
    node_type = obj.get("node_type") or obj.get("type") or ""
    if "sandbox_agent" in node_type or "sandbox" in node_type:
        # Check agent_prompt is not empty
        ap = obj.get("agent_prompt", "")
        if not ap:
            _fail(f"{path}: agent_prompt is empty in sandbox_agent node {prefix} — opencode will hang")

        # Check template_id is "opencode"
        tid = obj.get("template_id", "")
        if tid and tid != "opencode":
            _fail(
                f"{path}: template_id is '{tid}' in sandbox_agent node {prefix} — "
                "should be 'opencode' (has opencode CLI pre-installed)"
            )

        # Check timeout_seconds is not the default 600
        to = obj.get("timeout_seconds", 600)
        if to == 600:
            _fail(
                f"{path}: timeout_seconds is {to} (default) in sandbox_agent node {prefix} — "
                "increase to 1200 for complex tasks"
            )

    # Check env_vars ordering
    envs = obj.get("envs") or obj.get("env_vars")
    if isinstance(envs, dict) and "env_vars_extra" in envs:
        keys = list(envs.keys())
        extra_idx = keys.index("env_vars_extra")
        # Flag if env_vars_extra is not the first entry (it should be first to prevent overrides)
        for idx, key in enumerate(keys):
            if key in ("GITHUB_TOKEN", "APP_MODULO_OPENCODE_API_KEY", "GITHUB_REVIEWBOT_PAT") and extra_idx > idx:
                    _fail(
                        f"{path}: env_vars_extra (index {extra_idx}) is placed after "
                        f"'{key}' (index {idx}) in node {prefix} — "
                        "move env_vars_extra before system env vars to prevent override"
                    )

    # Recurse into children/edges
    for key in ("nodes", "edges", "items", "steps", "children"):
        children = obj.get(key)
        if isinstance(children, list):
            for i, child in enumerate(children):
                if isinstance(child, dict):
                    _check_node_config(path, child, f"{prefix}[{i}]")


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent

    # Scan source files
    node_runner = repo_root / "backend" / "src" / "modulo" / "core" / "pipeline_engine" / "node_runner.py"
    if node_runner.exists():
        _scan_node_runner(node_runner)

    mcp_server = repo_root / "backend" / "src" / "modulo" / "api" / "mcp_server.py"
    if mcp_server.exists():
        _scan_mcp_server(mcp_server)

    # Scan pipeline config files
    _scan_pipeline_config_files()

    if _EXIT_CODE:
        print("\nSome pipeline config validations failed — see messages above.", file=sys.stderr)
    else:
        print("All pipeline config checks passed.")

    return _EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
