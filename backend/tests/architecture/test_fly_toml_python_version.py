"""Architecture test: deployment Python image satisfies pyproject.toml's requires-python.

The backend Docker images pin an explicit ``FROM python:X.Y`` base. When the
project upgrades Python, the images and ``requires-python`` must agree. The
mutation-testing image and the all-in-one production image pin the same base,
so they are checked too.
"""

import re
from pathlib import Path

import pytest

PRODUCT = Path(__file__).resolve().parent.parent.parent.parent  # Product/
PYPROJECT_TOML = PRODUCT / "backend" / "pyproject.toml"
DOCKERFILES = (
    PRODUCT / "backend" / "Dockerfile",
    PRODUCT / "backend" / "Dockerfile.fly",
    PRODUCT / "backend" / "Dockerfile.mutation",
    PRODUCT / "deploy" / "docker" / "Dockerfile.all-in-one",
)

REQUIRES_PYTHON = re.compile(r'requires-python\s*=\s*">=3\.(\d+)(?:,<3\.(\d+))?"')
PYTHON_IMAGE = re.compile(r"^FROM\s+python:3\.(\d+)(?:-.*)?(?:\s+AS\s+[\w.-]+)?$", re.MULTILINE)


def test_dockerfile_python_version_satisfies_pyproject():
    if not PYPROJECT_TOML.exists():
        pytest.skip("backend/pyproject.toml not found")

    requires = REQUIRES_PYTHON.search(PYPROJECT_TOML.read_text(encoding="utf-8"))
    assert requires, "Could not parse requires-python in backend/pyproject.toml"
    min_minor = int(requires.group(1))
    max_minor = int(requires.group(2)) if requires.group(2) else None

    pinned = []
    for dockerfile in DOCKERFILES:
        if not dockerfile.exists():
            continue
        content = dockerfile.read_text(encoding="utf-8")
        pinned.extend((dockerfile.relative_to(PRODUCT), int(minor)) for minor in PYTHON_IMAGE.findall(content))

    if not pinned:
        pytest.skip("No python base image found in backend Dockerfiles")

    mismatches = [
        (rel, minor) for rel, minor in pinned if minor < min_minor or (max_minor is not None and minor >= max_minor)
    ]
    assert not mismatches, (
        "Deployment image uses a Python version outside backend/pyproject.toml's "
        f"requires-python range (>=3.{min_minor}"
        + (f",<3.{max_minor}" if max_minor is not None else "")
        + "): "
        + ", ".join(f"{rel} (python:3.{minor})" for rel, minor in mismatches)
    )
