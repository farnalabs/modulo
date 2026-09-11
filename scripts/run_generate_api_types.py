#!/usr/bin/env python3
"""Cross-platform wrapper for regenerating frontend TypeScript API types.

Replaces `frontend/scripts/generate-api-types.ps1` (invoked via
`pnpm run generate:api` and CI's schema-freshness job).

Behaviour (mirrors the `schema-freshness` job in `.github/workflows/ci.yml`):
1. Writes a temp Python script that imports ``modulo.api.main`` and dumps
   ``app.openapi()`` to a temp JSON file, with DATABASE_URL / SECRET_KEY /
   FERNET_KEY / MODULO_CSRF_ENABLED set to template values (so no real backend
   or DB is needed to build the schema - same template env CI uses).
2. Runs it from backend/ with the backend uv venv interpreter.
 3. Runs ``<pkg-manager> dlx openapi-typescript@7.13.0 <schema> --output <output>``
    from frontend/ (pnpm preferred, npm accepted as a fallback), version pinned
    to the one in frontend/package.json and the pnpm lockfile, matching CI
    exactly - a bare `npx --yes openapi-typescript` resolved unsupported
    releases and crashed on Node 22 with a broken @redocly core. Output:
    ``frontend/src/lib/api/schema.ts``.
4. Cleans up all temp files.

With ``--check`` (the local equivalent of CI's schema-freshness gate):
regenerates to a TEMP file only and fails non-zero with a diff summary if it
differs from the committed ``frontend/src/lib/api/schema.ts``. The committed
file is never modified in check mode.

Exit non-zero on failure.
"""

from __future__ import annotations

import contextlib
import difflib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = str(REPO_ROOT / "backend")
FRONTEND_DIR = str(REPO_ROOT / "frontend")
OUTPUT_FILE = str(REPO_ROOT / "frontend" / "src" / "lib" / "api" / "schema.ts")

# MUST match the pin in .github/workflows/ci.yml (`pnpm dlx
# openapi-typescript@7.13.0`) and frontend/package.json (^7.13.0).
OPENAPI_TYPESCRIPT_VERSION = "7.13.0"

_TEMPLATE_ENV = {
    "DATABASE_URL": "sqlite+aiosqlite:///TEMPLATE_DB",
    "SECRET_KEY": "a" * 32,
    "FERNET_KEY": "b" * 32,
    "MODULO_CSRF_ENABLED": "false",
}


def _run(cmd: list[str], cwd: str) -> int:
    print(f"  $ {' '.join(cmd)}", file=sys.stderr)
    # On Windows, `.cmd`/`.bat` shims (e.g. pnpm.cmd) cannot be launched directly
    # by CreateProcess (WinError 193/2); wrap them through the command
    # interpreter, mirroring run_frontend_npm.py.
    if sys.platform == "win32":
        exe = shutil.which(cmd[0])
        if exe and exe.lower().endswith((".cmd", ".bat")):
            cmd = ["cmd.exe", "/c", *cmd]
    return subprocess.run(cmd, cwd=cwd, check=False).returncode


def _find_pkg_manager() -> str | None:
    """Resolve the package manager binary used to drive `dlx`.

    pnpm is preferred; npm is accepted as a fallback (both support the `dlx`
    subcommand with the same openapi-typescript invocation). Returns the
    resolved binary so the command is actually built from it - not just gated.
    """
    if sys.platform == "win32":
        return shutil.which("pnpm.cmd") or shutil.which("pnpm") or shutil.which("npm.cmd") or shutil.which("npm")
    return shutil.which("pnpm") or shutil.which("npm")


def _generate_schema(tempdir: str) -> tuple[int, str]:
    """Dump app.openapi() and run openapi-typescript into tempdir/schema.ts.

    Returns (returncode, generated_path).
    """
    schema_path = str(Path(tempdir) / "openapi.json")
    db_path = str(Path(tempdir) / "gen-test.db")
    out_path = str(Path(tempdir) / "schema.ts")

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
        return rc, out_path

    if not Path(schema_path).is_file():
        print("Schema file was not created", file=sys.stderr)
        return 1, out_path

    print("=== Generating TypeScript types with openapi-typescript...")
    pkg_manager = _find_pkg_manager()
    if pkg_manager is None:
        print("Neither pnpm nor npm found on PATH", file=sys.stderr)
        return 1, out_path
    rc = _run(
        [pkg_manager, "dlx", f"openapi-typescript@{OPENAPI_TYPESCRIPT_VERSION}", schema_path, "--output", out_path],
        FRONTEND_DIR,
    )
    return rc, out_path


def main() -> int:
    check_mode = "--check" in sys.argv[1:]

    tempdir = tempfile.mkdtemp(prefix="modulo_gen_api_")
    py_script = str(Path(tempdir) / "gen_openapi.py")
    schema_path = str(Path(tempdir) / "openapi.json")
    db_path = str(Path(tempdir) / "gen-test.db")
    generated_path = str(Path(tempdir) / "schema.ts")
    try:
        rc, generated_path = _generate_schema(tempdir)
        if rc != 0:
            print("API type generation failed (see step messages above)", file=sys.stderr)
            return rc

        if check_mode:
            # CI-equivalent freshness gate: compare against the committed file
            # without ever touching it.
            committed = Path(OUTPUT_FILE)
            if not committed.is_file():
                print(f"Committed schema not found: {OUTPUT_FILE}", file=sys.stderr)
                return 1
            generated = Path(generated_path).read_text(encoding="utf-8")
            existing = committed.read_text(encoding="utf-8")
            if generated == existing:
                print("schema.ts is up to date", file=sys.stderr)
                return 0

            print("=== frontend/src/lib/api/schema.ts is STALE ===", file=sys.stderr)
            print("Run 'pnpm run generate:api' from frontend/ and commit the regenerated file.", file=sys.stderr)
            print("=== Diff summary (committed -> generated):", file=sys.stderr)
            diff = list(
                difflib.unified_diff(
                    existing.splitlines(keepends=True),
                    generated.splitlines(keepends=True),
                    fromfile="committed/schema.ts",
                    tofile="generated/schema.ts",
                )
            )
            for line in diff[:80]:
                print(f"  {line}", file=sys.stderr, end="" if line.endswith("\n") else "\n")
            if len(diff) > 80:
                print(f"  ... ({len(diff) - 80} more diff lines truncated)", file=sys.stderr)
            print(f"({len(diff)} diff lines total)", file=sys.stderr)
            return 1

        # Write mode: move the generated file into place atomically.
        out = Path(OUTPUT_FILE)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(generated_path, out)
    finally:
        # Clean up temp files (schema, script, temp DB).
        for candidate in (py_script, schema_path, db_path, generated_path):
            with contextlib.suppress(OSError):
                Path(candidate).unlink()
        with contextlib.suppress(OSError):
            Path(tempdir).rmdir()

    print(f"=== Done: {OUTPUT_FILE}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
