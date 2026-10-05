"""Client separation: enforced in code, never by instruction to the model."""

from __future__ import annotations

import dataclasses

import pytest
from sqlalchemy import select

from personal_agent.clients import ClientScope, ClientStore, CrossClientAccess
from personal_agent.db import Database, audit_log, tool_calls
from personal_agent.gateway.tools import ToolClass, ToolGateway, ToolRefused, ToolSpec


@pytest.fixture
def store(settings):
    return ClientStore(settings.clients_dir, Database(settings.database_url))


def test_cross_client_path_refused_and_audited(store):
    acme = store.ensure("acme")
    store.ensure("beta")
    store.store_file(acme, "brief.md", b"# Acme brief\nobjective: launch")

    assert b"Acme" in store.read_bytes(acme, "brief.md")
    with pytest.raises(CrossClientAccess):
        store.read_bytes(acme, "../beta/brief.md")

    from sqlalchemy import select
    from personal_agent.db import audit_log

    with store.db.engine.connect() as cx:
        audits = [dict(r) for r in cx.execute(select(audit_log)).mappings()]
    assert any(a["action"] == "cross_client_refused" for a in audits)


def test_retrieval_never_crosses_folders(store):
    acme = store.ensure("acme")
    beta = store.ensure("beta")
    store.store_file(acme, "notes.md", b"acme is planning a Q2 campaign")
    store.store_file(beta, "secret.md", b"the beta-only answer is 42")

    assert store.search(acme, "beta-only") == []
    assert store.search(acme, "42") == []
    hits = store.search(beta, "42")
    assert len(hits) == 1 and "beta-only answer" in hits[0]["excerpt"]


def test_gateway_refuses_tool_payload_naming_another_client(settings):
    db = Database(settings.database_url)
    gw = ToolGateway(db)
    gw.register(ToolSpec("read_client_file", ToolClass.READ, fn=lambda p: {"ok": True}))
    db.create_run("run-10", "foundation-test", "acme", {}, "v0")

    with pytest.raises(ToolRefused) as e:
        gw.call("run-10", "intake", ["read_client_file"], "read_client_file",
                {"client": "beta", "path": "secret.md"})
    assert "cross-client" in str(e.value) or "run scope" in str(e.value)

    with db.engine.connect() as cx:
        calls = [dict(r) for r in cx.execute(select(tool_calls)).mappings()]
        audits = [dict(r) for r in cx.execute(select(audit_log)).mappings()]
    assert calls[-1]["outcome"] == "refused"
    assert any(a["action"] == "cross_client_refused" for a in audits)

    # The run's own client is fine.
    assert gw.call("run-10", "intake", ["read_client_file"], "read_client_file",
                   {"client": "acme", "path": "brief.md"}) == {"ok": True}


def test_client_scope_is_immutable():
    scope = ClientScope("acme")
    with pytest.raises(dataclasses.FrozenInstanceError):
        scope.name = "beta"  # type: ignore[misc]


def test_start_run_provisions_the_client_folder(settings):
    from tests.conftest import make_engine

    engine = make_engine(settings, [])
    run_id = engine.start_run("foundation-test", "newco", {"objective": "x"})
    assert engine.db.get_run(run_id)["client"] == "newco"
    assert (settings.clients_dir / "newco").is_dir()
