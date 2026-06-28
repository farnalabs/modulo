FROM python:3.12-slim

WORKDIR /app

RUN pip install uv

COPY pyproject.toml alembic.ini entrypoint.sh ./
COPY src/ src/
RUN uv pip install --system -e .
RUN chmod +x entrypoint.sh

CMD ["./entrypoint.sh"]
