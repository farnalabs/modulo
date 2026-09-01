#!/usr/bin/env python3
"""Cross-platform wrapper for regenerating frontend TypeScript API types.

Replaces `frontend/scripts/generate-api-types.ps1` (invoked via
`pnpm run generate:api` and the pre-commit manual-stage hook).

Behaviour:
1. Writes a temp Python script that imports ``modulo.api.main`` and dumps
   ``app.openapi()`` to a temp JSON file, with DATABASE_URL / SECRET_KEY /
   FERNET_KEY / MODULO_CSRF_ENABLED set to template values (so no real backend
   or DB is needed to build the schema).
2. Runs it from backend/ with ``python``.
3. Runs ``npx --yes openapi-typescript <schema> --output <output>`` from
   frontend/ to produce ``frontend/src/lib/api/schema.ts``.
4. Cleans up all temp files.

Exit non-zero on failure.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
BACKEND_DIR = str(Path(REPO_ROOT) / "backend")
FRONTEND_DIR = str(Path(REPO_ROOT) / "frontend")
OUTPUT_FILE = str(Path(FRONTEND_DIR) / "src" / "lib" / "api" / "schema.ts")

_TEMPLATE_ENV = {
    "DATABASE_URL": "sqlite+aiosqlite:///TEMPLATE_DB",
    "SECRET_KEY": "a" * 32,
    "FERNET_KEY": "b" * 32,
    "MODULO_CSRF_ENABLED": "false",
}


def _run(cmd: list[str], cwd: str) -> int:
    print(f"  $ {' '.join(cmd)}", file=sys.stderr)
    # On Windows, `.cmd`/`.bat` shims (e.g. npx.cmd) cannot be launched directly
    # by CreateProcess (WinError 193/2); wrap them through the command
    # interpreter, mirroring run_frontend_npm.py.
    if sys.platform == "win32":
        exe = shutil.which(cmd[0])
        if exe and exe.lower().endswith((".cmd", ".bat")):
            cmd = ["cmd.exe", "/c", *cmd]
    return subprocess.run(cmd, cwd=cwd).returncode


def main() -> int:
    tempdir = tempfile.mkdtemp(prefix="modulo_gen_api_")
    try:
        schema_path = str(Path(tempdir) / "openapi.json")
        db_path = str(Path(tempdir) / "gen-test.db")

        py_script = str(Path(tempdir) / "gen_openapi.py")
        py_content = (
            "import os, json, sys\n"
            "sys.tracebacklimit = 0\n"
            f'os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///{db_path.replace(os.sep, "/")}"\n'
            f'os.environ["SECRET_KEY"] = "{_TEMPLATE_ENV["SECRET_KEY"]}"\n'
            f'os.environ["FERNET_KEY"] = "{_TEMPLATE_ENV["FERNET_KEY"]}"\n'
            f'os.environ["MODULO_CSRF_ENABLED"] = "{_TEMPLATE_ENV["MODULO_CSRF_ENABLED"]}"\n'
            "from modulo.api.main import app\n"
            f'with open(r"{schema_path.replace(os.sep, "/")}", "w", encoding="utf-8") as f:\n'
            "    json.dump(app.openapi(), f, ensure_ascii=False)\n"
            f'print(f"Schema: {{os.path.getsize(r"{schema_path.replace(os.sep, "/")}")}} bytes")\n'
        )
        with Path(py_script).open("w", encoding="utf-8") as fh:
            fh.write(py_content)

        print("=== Generating OpenAPI schema from backend...")
        # Use the interpreter running this wrapper (the backend uv venv python
        # when invoked via `uv run --project backend`) so `modulo` resolves —
        # not whatever bare `python` happens to be first on PATH.
        rc = _run([sys.executable, py_script], BACKEND_DIR)
        if rc != 0:
            print("Backend schema generation failed", file=sys.stderr)
            return rc

        if not Path(schema_path).is_file():
            print("Schema file was not created", file=sys.stderr)
            return 1

        print("=== Generating TypeScript types with openapi-typescript...")
        rc = _run(
            ["npx", "--yes", "openapi-typescript", schema_path, "--output", OUTPUT_FILE],
            FRONTEND_DIR,
        )
        if rc != 0:
            print("openapi-typescript failed", file=sys.stderr)
            return rc
    finally:
        # Clean up temp files (schema, script, temp DB).
        for candidate in (py_script, schema_path, db_path):
            with contextlib.suppress(OSError):
                Path(candidate).unlink()
        with contextlib.suppress(OSError):
            Path(tempdir).rmdir()

    print(f"=== Done: {OUTPUT_FILE}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
