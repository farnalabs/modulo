"""Architecture test: every [build] dockerfile reference in deploy/fly/*.toml resolves.

Flyctl resolves the `[build] dockerfile` option relative to the config file's
own directory (not the Docker build context). When a fly.toml lives in a
subdirectory (e.g. deploy/fly/fly.staging.toml), a path that is correct from
the repo root is WRONG from the toml's directory. This test fails the build at
CI time — instead of at deploy time — if any dockerfile reference does not
resolve to an existing file relative to its own toml directory.

Regression guard for FAR-434: fly.staging.toml was moved from the repo root to
deploy/fly/, which silently broke the previously-root-relative dockerfile path.
"""

import re
from pathlib import Path

PRODUCT = Path(__file__).resolve().parent.parent.parent.parent  # Product/
FLY_TOML_DIR = PRODUCT / "deploy" / "fly"

DOCKERFILE_REF = re.compile(
    r"^\s*dockerfile\s*=\s*[\"']([^\"']+)[\"']",
    re.MULTILINE | re.IGNORECASE,
)


def _collect_tomls():
    if not FLY_TOML_DIR.exists():
        return []
    return sorted(FLY_TOML_DIR.glob("*.toml"))


def test_fly_toml_dockerfile_references_resolve():
    tomls = _collect_tomls()
    if not tomls:
        return

    failures = []
    for toml in tomls:
        content = toml.read_text(encoding="utf-8")
        for match in DOCKERFILE_REF.finditer(content):
            ref = match.group(1)
            resolved = (toml.parent / ref).resolve()
            if not resolved.exists():
                failures.append(
                    f'{toml.relative_to(PRODUCT)}: dockerfile = "{ref}" '
                    f"does not resolve (looked for {resolved.relative_to(PRODUCT)})"
                )

    assert not failures, (
        "One or more deploy/fly/*.toml [build] dockerfile references do not "
        "resolve relative to their own directory (flyctl resolves this path "
        "relative to the toml file, not the build context):\n" + "\n".join(failures)
    )
