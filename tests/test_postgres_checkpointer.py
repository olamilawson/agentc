"""Deployment gate: on Postgres, a run pauses for approval and resumes after a restart.

Skipped unless PERSONAL_AGENT_DATABASE_URL is a Postgres URL, as it is inside the
api container on the VPS (README, Deployment). The test works in a scratch
database it creates on that server and drops afterwards; the real one is not
touched.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy.engine import make_url

from personal_agent.engine.runner import Engine, postgres_conninfo
from tests.conftest import INPUTS, draft_json, make_engine

URL = os.environ.get("PERSONAL_AGENT_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not URL.startswith("postgresql"), reason="PERSONAL_AGENT_DATABASE_URL is not Postgres"
)


@pytest.fixture
def pg_settings(settings):
    import psycopg

    name = f"pa_test_{uuid.uuid4().hex[:12]}"
    server = postgres_conninfo(URL)
    with psycopg.connect(server, autocommit=True) as cx:
        cx.execute(f'CREATE DATABASE "{name}"')
    scratch = make_url(URL).set(database=name).render_as_string(hide_password=False)
    try:
        yield settings.model_copy(update={"database_url": scratch})
    finally:
        with psycopg.connect(server, autocommit=True) as cx:
            cx.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _shut_down(engine: Engine) -> None:
    engine.checkpointer.conn.close()  # the connection pool
    engine.db.engine.dispose()


def test_pause_resume_on_postgres(pg_settings):
    from langgraph.checkpoint.postgres import PostgresSaver

    # Process one: start a run; it must stop at the approval queue.
    first = make_engine(pg_settings, [draft_json()])
    assert isinstance(first.checkpointer, PostgresSaver)
    run_id = first.start_run("foundation-test", "acme", INPUTS)
    assert first.db.get_run(run_id)["status"] == "waiting_for_approval"
    content_hash = first.pending_approval(run_id)["content_hash"]
    _shut_down(first)

    # Process two (after a restart): a fresh engine finds the checkpoint in
    # Postgres, shows the same content and resumes from it.
    second = make_engine(pg_settings, [])
    try:
        assert second.pending_approval(run_id)["content_hash"] == content_hash
        run = second.approve(run_id, "approved", None)
        assert run["status"] == "done"
        assert run["outputs"]["released"]["sent"] is True
    finally:
        _shut_down(second)
