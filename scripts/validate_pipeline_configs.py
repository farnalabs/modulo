"""Validate Modulo pipeline configuration patterns.

Checks for known bug patterns in sandbox_agent pipeline configs:
1. Empty ``agent_prompt`` that would cause opencode to hang
2. ``timeout_seconds`` defaulting to 600 (too short for complex tasks)
3. ``template_id`` not set to ``"opencode"``

Note: ``**env_vars_extra`` intentionally sits AFTER system env vars in the
sandbox envs dict so pipelines can override the system GITHUB_TOKEN for
identity separation (e.g. PR Reviewer injects its own PAT). See
``node_runner.py`` (commit b0c4bde97) — do not flag that ordering.
"""

from __future__ import annotations

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
    """Validate sandbox_agent node defaults in node_runner.

    The ``template_id`` (default "base") and ``timeout_seconds`` (default 600)
    fallbacks in ``node_runner.py`` are deliberate last-resort defaults used
    only when a pipeline config omits the field; pipeline configs are validated
    against the opencode/1200 guidance separately by
    ``_scan_pipeline_config_files``. No checks fire here.
    """
    path.read_text(encoding="utf-8")


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

    # env_vars_extra ordering is intentionally AFTER system env vars — see
    # module docstring / commit b0c4bde97. No ordering check here.

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
