"""The Canva and Higgsfield shells: tools that exist, obey the gateway, and refuse every call for now."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from personal_agent import connectors
from personal_agent.api.main import create_app
from personal_agent.db import Database
from personal_agent.engine.runner import Engine
from personal_agent.gateway.tools import ApprovalRequired, ToolRefused
from personal_agent.harness.models import RecordedGateway

NAMES = ["CANVA_CLIENT_ID", "CANVA_CLIENT_SECRET", "HIGGSFIELD_API_KEY"]


@pytest.fixture
def engine(settings, monkeypatch) -> Engine:
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)
    return Engine(settings, Database(settings.database_url), gateway=RecordedGateway({}))


def _run(engine: Engine, client: str = "acme") -> str:
    return engine.start_run("foundation-test", client, {"objective": "x"}, work=False)


def test_each_connector_registers_its_tools_with_their_classes(engine):
    registered = dict(engine.harness.tools.registered())
    assert registered["canva_create_design"] == "draft" and registered["canva_share_design"] == "release"
    assert registered["higgsfield_generate_image"] == "draft" and registered["higgsfield_generate_video"] == "draft"


def test_a_call_stops_with_the_reason_not_connected_then_not_built(engine, monkeypatch):
    run_id, tools = _run(engine), engine.harness.tools
    call = lambda: tools.call(run_id, "make", ["higgsfield_generate_image"], "higgsfield_generate_image",  # noqa: E731
                              {"client": "acme", "prompt": "a poster"})
    with pytest.raises(connectors.ConnectorNotReady, match="not connected. The server has no HIGGSFIELD_API_KEY"):
        call()
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test-key-not-real")
    with pytest.raises(connectors.ConnectorNotReady, match="is not built yet"):
        call()
    assert [t["outcome"] for t in engine.db.list_tool_calls(run_id)] == ["failed", "failed"]


def test_the_gateways_rules_hold_for_connector_tools(engine):
    run_id, tools = _run(engine), engine.harness.tools
    with pytest.raises(ToolRefused):  # not on the node's allowlist
        tools.call(run_id, "make", [], "canva_create_design", {"client": "acme"})
    with pytest.raises(ToolRefused):  # another client's name
        tools.call(run_id, "make", ["canva_create_design"], "canva_create_design", {"client": "other"})
    with pytest.raises(ApprovalRequired):  # a release with no approval never reaches the connector
        tools.call(run_id, "make", ["canva_share_design"], "canva_share_design", {"client": "acme", "to": "x@example.com"})


def test_a_restricted_clients_work_stays_out_of_an_unapproved_connector(engine, monkeypatch):
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test-key-not-real")
    run_id = _run(engine)
    engine.db.set_restricted("acme", True)
    with pytest.raises(connectors.ConnectorNotReady, match="acme is restricted and Higgsfield is not approved"):
        engine.harness.tools.call(run_id, "make", ["higgsfield_generate_image"], "higgsfield_generate_image", {"client": "acme"})


def test_settings_screen_shows_each_connector_and_its_state(settings, monkeypatch):
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test-key-not-real")
    page = TestClient(create_app(settings, gateway=RecordedGateway({}))).get("/cost").text
    assert "Not connected: the server has no CANVA_CLIENT_ID or CANVA_CLIENT_SECRET." in page
    assert "Settings saved. The calls to the service are not built yet." in page
    assert "<code>canva_share_design</code> (release)" in page and "test-key" not in page
