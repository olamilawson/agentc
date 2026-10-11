FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN uv venv && uv pip install --system . "langgraph-checkpoint-postgres>=2.0" "psycopg[binary]>=3.1"

# The package is installed into site-packages, so config.py's REPO_ROOT does not
# point at /app here: every path it would derive from REPO_ROOT is set explicitly.
COPY config ./config
COPY skills ./skills
ENV PERSONAL_AGENT_ROUTE_TABLE=/app/config/route_table.yaml \
    PERSONAL_AGENT_BUDGETS=/app/config/budgets.yaml \
    PERSONAL_AGENT_SPONSORSHIP=/app/config/sponsorship.yaml \
    PERSONAL_AGENT_SKILLS_DIR=/app/skills \
    PERSONAL_AGENT_CLIENTS_DIR=/app/data/clients \
    PERSONAL_AGENT_CHECKPOINT_DB=/app/data/checkpoints.sqlite

EXPOSE 8000
CMD ["uvicorn", "personal_agent.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
