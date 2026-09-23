# syntax=docker/dockerfile:1.27
FROM python:3.14-slim

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    UV_LINK_MODE=copy

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends postgresql-client
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --only-binary :all: uv==0.11.13

COPY pyproject.toml uv.lock ./
# --extra docker (FAR-1201): installs aiodocker — required by the SAQ system
# worker's runner_workspace_reconcile sweep and the DockerRuntimeProvider.
# docker-compose (saq-system) and the Helm chart run
# `python -m modulo.core.saq_worker` from THIS image, so without the extra
# every reconcile tick fails with "No module named 'aiodocker'" and the
# advisory runner_workspace_reconcile check on /healthz/ready is permanently
# degraded (matches backend/Dockerfile.fly, which already passes the extra).
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra docker --no-install-project

COPY alembic.ini entrypoint.sh ./
COPY src/ src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra docker && \
    uv pip install --only-binary :all: psycopg2-binary==2.9.12 && \
    chmod +x entrypoint.sh

RUN useradd --create-home --uid 10001 appuser && \
    chown -R appuser:appuser /app
USER appuser

CMD ["./entrypoint.sh"]
