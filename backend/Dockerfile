FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y postgresql-client && rm -rf /var/lib/apt/lists/*
RUN pip install uv

COPY pyproject.toml alembic.ini entrypoint.sh ./
COPY src/ src/
RUN uv pip install --system -e . psycopg2-binary
RUN chmod +x entrypoint.sh

CMD ["./entrypoint.sh"]
