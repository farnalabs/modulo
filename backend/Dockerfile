FROM python:3.12-slim

WORKDIR /app

RUN pip install uv

COPY pyproject.toml alembic.ini ./
COPY src/ src/
# Install the sync PostgreSQL driver for Alembic migrations.
# env.py runs migrations synchronously (psycopg2) to avoid event-loop
# conflicts with the main app's running async event loop.
RUN uv pip install --system -e . psycopg2-binary

CMD ["uvicorn", "modulo.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
