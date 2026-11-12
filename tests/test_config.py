from __future__ import annotations

import re
import shutil

from personal_agent.config import REPO_ROOT, load_settings


def test_every_repo_relative_path_can_be_set_from_the_environment(tmp_path, monkeypatch):
    """The Docker image installs the package into site-packages, where REPO_ROOT
    is not the app directory: settings must load from the environment alone."""
    shutil.copytree(REPO_ROOT / "config", tmp_path / "config")
    shutil.copytree(REPO_ROOT / "skills", tmp_path / "skills")
    env = {
        "PERSONAL_AGENT_DATABASE_URL": f"sqlite:///{tmp_path / 'data' / 'app.db'}",
        "PERSONAL_AGENT_ROUTE_TABLE": tmp_path / "config" / "route_table.yaml",
        "PERSONAL_AGENT_BUDGETS": tmp_path / "config" / "budgets.yaml",
        "PERSONAL_AGENT_SPONSORSHIP": tmp_path / "config" / "sponsorship.yaml",
        "PERSONAL_AGENT_SKILLS_DIR": tmp_path / "skills",
        "PERSONAL_AGENT_CLIENTS_DIR": tmp_path / "data" / "clients",
        "PERSONAL_AGENT_CHECKPOINT_DB": tmp_path / "data" / "checkpoints.sqlite",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, str(value))

    settings = load_settings()

    assert settings.skills_dir == tmp_path / "skills"
    assert settings.clients_dir == tmp_path / "data" / "clients"
    assert settings.checkpoint_path == tmp_path / "data" / "checkpoints.sqlite"
    assert settings.route_table
    paths = [settings.skills_dir, settings.clients_dir, settings.checkpoint_path]
    assert not any(p.is_relative_to(REPO_ROOT) for p in paths)


def test_skills_dir_defaults_to_the_repository(monkeypatch):
    monkeypatch.delenv("PERSONAL_AGENT_SKILLS_DIR", raising=False)
    assert load_settings().skills_dir == REPO_ROOT / "skills"


def test_checkpoints_follow_the_database(settings):
    from langgraph.checkpoint.sqlite import SqliteSaver

    from personal_agent.engine.runner import open_checkpointer, postgres_conninfo

    assert postgres_conninfo(settings.database_url) is None
    assert isinstance(open_checkpointer(settings), SqliteSaver)
    # The compose file's SQLAlchemy URL, as libpq wants it; the password stays encoded.
    assert (
        postgres_conninfo("postgresql+psycopg://personal_agent:p%40ss@postgres:5432/personal_agent")
        == "postgresql://personal_agent:p%40ss@postgres:5432/personal_agent"
    )


def test_every_config_file_the_app_reads_is_named_in_the_image():
    # Inside the image the package cannot find config/ by itself, so each file needs its line in the Dockerfile.
    source = (REPO_ROOT / "src" / "personal_agent" / "config.py").read_text(encoding="utf-8")
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    variables = set(re.findall(r'from_yaml\("(PERSONAL_AGENT_\w+)"', source))
    variables |= set(re.findall(r'"(PERSONAL_AGENT_\w+)"[^\n]*REPO_ROOT / "config"', source))
    assert "PERSONAL_AGENT_ENQUIRIES" in variables and "PERSONAL_AGENT_ROUTE_TABLE" in variables
    missing = sorted(v for v in variables if f"{v}=/app/config/" not in dockerfile)
    assert missing == []
