FROM python:3.12-slim

WORKDIR /app

RUN pip install uv

COPY pyproject.toml alembic.ini ./
COPY src/ src/
RUN uv pip install --system -e .

CMD ["uvicorn", "modulo.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
