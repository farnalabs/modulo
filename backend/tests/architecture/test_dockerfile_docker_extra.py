"""Architecture test: SAQ-worker images install the `docker` extra (FAR-1201).

The backend image is not just the API: docker-compose's ``saq-system``
service and the Helm chart's ``saqSystem`` deployment both run
``python -m modulo.core.saq_worker`` from the SAME image built by
``backend/Dockerfile`` (Fly runs the system worker in-process from
``backend/Dockerfile.fly``). Always-registered system crons import
``aiodocker`` lazily — notably:

- ``runner_workspace_reconcile`` (``core/bundled_runner/runner_reconciler.py``)
- ``runner_health_probe`` (``core/bundled_runner/health_probe.py``)

``aiodocker`` is declared only under the ``docker`` optional extra, so a
``uv sync --no-dev`` WITHOUT ``--extra docker`` ships an image where every
reconcile tick dies with ``ModuleNotFoundError: No module named 'aiodocker'``
and the advisory ``runner_workspace_reconcile`` check on ``/healthz/ready``
is permanently degraded (observed on the FAR-1052 EKS validation —
``backend/Dockerfile`` was the only SAQ-worker image missing the extra).
"""

import re
import tomllib
from pathlib import Path

PRODUCT = Path(__file__).resolve().parent.parent.parent.parent
PYPROJECT = PRODUCT / "backend" / "pyproject.toml"

# Images whose process tree runs the SAQ system worker (owner of the
# runner_workspace_reconcile cron). The all-in-one image is excluded: its
# supervisord runs only uvicorn + nginx, never the system worker.
SAQ_SYSTEM_WORKER_DOCKERFILES = (
    PRODUCT / "backend" / "Dockerfile",
    PRODUCT / "backend" / "Dockerfile.fly",
)

_UV_SYNC = re.compile(r"uv sync\b[^\n]*")


def _uv_sync_commands(dockerfile: Path) -> list[str]:
    """Every non-comment ``uv sync`` invocation in *dockerfile*."""
    text = dockerfile.read_text(encoding="utf-8")
    code_lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    return [match.strip() for match in _UV_SYNC.findall("\n".join(code_lines))]


def test_saq_worker_images_install_docker_extra() -> None:
    """Every ``uv sync`` in an SAQ-worker image requests ``--extra docker``.

    Guards FAR-1201: without the extra the image lacks ``aiodocker`` and the
    runner_workspace_reconcile readiness check degrades with an import error.
    """
    violations: list[str] = []
    for dockerfile in SAQ_SYSTEM_WORKER_DOCKERFILES:
        rel = dockerfile.relative_to(PRODUCT)
        commands = _uv_sync_commands(dockerfile)
        assert commands, f"{rel} has no uv sync invocations"
        for command in commands:
            if "--extra docker" not in command:
                violations.append(f"{rel}: {command}")
    assert not violations, (
        "uv sync must pass --extra docker (FAR-1201): the SAQ system worker's "
        "runner_workspace_reconcile / runner_health_probe crons import aiodocker, "
        "which ships only via the docker extra — without it the advisory "
        f"/healthz/ready check degrades with ModuleNotFoundError. Violations: {violations}"
    )


def test_docker_extra_declares_aiodocker() -> None:
    """``--extra docker`` must actually pull aiodocker (the flag is not a no-op)."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    docker_extra = data["project"]["optional-dependencies"]["docker"]
    assert any(req.startswith("aiodocker") for req in docker_extra), (
        f"[project.optional-dependencies].docker must declare aiodocker, got: {docker_extra}"
    )
