"""Architecture test: fly.toml Python version matches pyproject.toml.

Hardcoded 'python3.12' in fly.toml's SSH commands becomes stale when
the project upgrades Python. Both files must agree.
"""

import re
from pathlib import Path

PRODUCT = Path(__file__).resolve().parent.parent.parent.parent  # Product/
FLY_TOML = PRODUCT / "fly.toml"
PYPROJECT_TOML = PRODUCT / "backend" / "pyproject.toml"


def test_fly_toml_python_version_matches_pyproject():
    if not FLY_TOML.exists():
        return
    if not PYPROJECT_TOML.exists():
        return

    fly_content = FLY_TOML.read_text(encoding="utf-8")
    pyproject_content = PYPROJECT_TOML.read_text(encoding="utf-8")

    fly_versions = set(re.findall(r"python3\.(\d+)", fly_content))
    pyproject_match = re.search(r'requires-python\s*=\s*">=3\.(\d+)"', pyproject_content)

    assert pyproject_match, "Could not find requires-python in pyproject.toml"
    py_major = pyproject_match.group(1)

    mismatches = [v for v in fly_versions if v != py_major]
    assert not mismatches, (
        f"fly.toml references Python 3.{', '.join(mismatches)} "
        f"but pyproject.toml requires Python >=3.{py_major}. "
        "Update the hardcoded version strings in fly.toml."
    )
