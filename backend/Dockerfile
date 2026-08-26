# syntax=docker/dockerfile:1.26
FROM python:3.12-slim

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    UV_LINK_MODE=copy

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends postgresql-client
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --only-binary :all: uv==0.11.13

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY alembic.ini entrypoint.sh ./
COPY src/ src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev && \
    uv pip install --only-binary :all: psycopg2-binary==2.9.12 && \
    chmod +x entrypoint.sh

RUN useradd --create-home --uid 10001 appuser && \
    chown -R appuser:appuser /app
USER appuser

CMD ["./entrypoint.sh"]
