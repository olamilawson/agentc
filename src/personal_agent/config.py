"""Settings and configuration loading.

Route table and budgets are version-controlled files under config/; secrets are
never in them. Database and checkpoint locations come from the environment so
the same code runs on SQLite locally and Postgres on the VPS.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[2]


class TierRoute(BaseModel):
    primary: str
    fallback: str | None = None


class Budgets(BaseModel):
    max_total_tokens: int = 200_000
    max_tool_calls: int = 20
    max_cost_usd: float = 5.0
    max_elapsed_seconds: int = 600


class Settings(BaseModel):
    database_url: str
    checkpoint_path: Path
    route_table: dict[str, TierRoute]
    budgets: Budgets
    skills_dir: Path


def load_settings() -> Settings:
    db = os.environ.get(
        "PERSONAL_AGENT_DATABASE_URL", f"sqlite:///{REPO_ROOT / 'data' / 'app.db'}"
    )
    ckpt = Path(
        os.environ.get(
            "PERSONAL_AGENT_CHECKPOINT_DB", str(REPO_ROOT / "data" / "checkpoints.sqlite")
        )
    )
    if db.startswith("sqlite:///"):
        Path(db.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    ckpt.parent.mkdir(parents=True, exist_ok=True)

    route_path = Path(os.environ.get("PERSONAL_AGENT_ROUTE_TABLE", REPO_ROOT / "config" / "route_table.yaml"))
    budget_path = Path(os.environ.get("PERSONAL_AGENT_BUDGETS", REPO_ROOT / "config" / "budgets.yaml"))
    routes = {tier: TierRoute(**r) for tier, r in yaml.safe_load(route_path.read_text()).items()}
    budgets = Budgets(**yaml.safe_load(budget_path.read_text()))
    return Settings(
        database_url=db,
        checkpoint_path=ckpt,
        route_table=routes,
        budgets=budgets,
        skills_dir=REPO_ROOT / "skills",
    )
