FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN uv venv && uv pip install --system . "langgraph-checkpoint-postgres>=2.0" "psycopg[binary]>=3.1"

EXPOSE 8000
CMD ["uvicorn", "personal_agent.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
