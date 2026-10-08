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


class SponsorshipPolicy(BaseModel):
    """The owner's thresholds for sponsorship triage (config/sponsorship.yaml)."""

    pursue_at: float
    decline_below: float
    # Naira. None: every enquiry that names an amount escalates.
    escalate_above: float | None = None


class Settings(BaseModel):
    database_url: str
    checkpoint_path: Path
    route_table: dict[str, TierRoute]
    budgets: Budgets
    skills_dir: Path
    clients_dir: Path
    # None: no policy is configured and every sponsorship enquiry escalates.
    sponsorship: SponsorshipPolicy | None = None
    # Shared secret for the n8n webhook; None disables the webhook.
    n8n_secret: str | None = None


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
    policy_path = Path(os.environ.get("PERSONAL_AGENT_SPONSORSHIP", REPO_ROOT / "config" / "sponsorship.yaml"))
    policy = SponsorshipPolicy(**yaml.safe_load(policy_path.read_text())) if policy_path.exists() else None
    return Settings(
        sponsorship=policy,
        n8n_secret=os.environ.get("PERSONAL_AGENT_N8N_SECRET") or None,
        database_url=db,
        checkpoint_path=ckpt,
        route_table=routes,
        budgets=budgets,
        skills_dir=REPO_ROOT / "skills",
        clients_dir=Path(
            os.environ.get("PERSONAL_AGENT_CLIENTS_DIR", str(REPO_ROOT / "data" / "clients"))
        ),
    )
