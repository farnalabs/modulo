"""FAR-366 - Helm image tags default to the chart's appVersion, never ``latest``.

The chart used to default every workload image to the mutable ``latest`` tag (a
SonarQube code smell: an install can silently change image on the next pull).
Pinning a literal version into ``values.yaml`` was also rejected: the literal
drifts from ``Chart.yaml`` ``appVersion`` -- which ``scripts/bump-version.py``
maintains fail-loud -- and it pointed at a tag GHCR had not published, so a
default-values install regressed to ``ImagePullBackOff``.

The chart must therefore have a single source of truth: an empty ``tag``
resolves to the chart's own ``appVersion``, and every workload template passes
``.Chart.AppVersion`` into the image helper (``.Chart`` is not in scope inside a
dict-call helper).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_CHART = _REPO_ROOT / "deploy" / "helm" / "modulo"

_IMAGE_KEYS = ("backend", "saqRunner", "saqSystem", "frontend")
_WORKLOAD_TEMPLATES = (
    "backend-deployment.yaml",
    "saq-runner-deployment.yaml",
    "saq-system-deployment.yaml",
    "frontend-deployment.yaml",
)


def _values() -> dict[str, Any]:
    data = yaml.safe_load((_CHART / "values.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_default_image_tags_resolve_from_the_chart_not_a_literal() -> None:
    values = _values()
    chart_app_version = str(yaml.safe_load((_CHART / "Chart.yaml").read_text(encoding="utf-8"))["appVersion"])
    for key in _IMAGE_KEYS:
        tag = values[key]["image"]["tag"]
        assert tag != "latest", (
            f"values.yaml {key}.image.tag defaults to the mutable 'latest' tag; "
            "leave it empty so it resolves to Chart.yaml appVersion"
        )
        assert tag == "", (
            f"values.yaml {key}.image.tag is {tag!r}; a literal version drifts from "
            "Chart.yaml appVersion (which bump-version.py maintains). Leave it empty "
            f"to resolve to appVersion {chart_app_version!r}"
        )


def test_image_helper_defaults_to_chart_app_version() -> None:
    helper = (_CHART / "templates" / "_helpers.tpl").read_text(encoding="utf-8")
    assert "(.image.tag | default .appVersion)" in helper, (
        "the modulo.image helper must default an empty tag to .appVersion"
    )
    assert not re.search(r'default "\d+\.\d+\.\d+"', helper), (
        "the modulo.image helper hardcodes a version literal instead of using the "
        "chart's appVersion; bump-version.py cannot keep that literal in sync"
    )


def test_workload_templates_pass_app_version_into_the_image_helper() -> None:
    for filename in _WORKLOAD_TEMPLATES:
        text = (_CHART / "templates" / filename).read_text(encoding="utf-8")
        assert '"appVersion" .Chart.AppVersion' in text, (
            f"{filename} includes modulo.image without passing appVersion; the helper "
            "has no .Chart in scope, so an empty tag would render empty"
        )
