"""My Studio, "Ask Ijeoma.": the owner's home from the reference boards, on real data."""

from __future__ import annotations

from fastapi.testclient import TestClient

from personal_agent.api.main import create_app
from personal_agent.db import Database
from personal_agent.harness.models import RecordedGateway


def _client(settings, **responses) -> TestClient:
    return TestClient(create_app(settings, gateway=RecordedGateway(responses)), follow_redirects=False)


def test_the_studio_greets_with_the_command_box_and_three_agents(settings):
    page = _client(settings).get("/").text
    assert "Ask Ijeoma" in page and 'id="studio-command"' in page and 'action="/think"' in page
    assert 'enctype="multipart/form-data"' in page and 'name="files"' in page
    assert page.count('<li data-state="available">') == 2 and '<li data-state="not_set_up">' in page  # the Builder
    assert "Nothing waits for your decision." in page and '<section class="card" data-state="clear">' in page
    assert 'aria-current="page"' in page and ">My Studio<" in page


def test_the_decision_queue_and_counts_come_from_the_data(settings):
    db = Database(settings.database_url)
    db.create_task(title="Approve the launch memo", workstream="AltBank", status="awaiting_approval", risk=None)
    db.create_enquiry(status="received", origin="request", first_name="Ada", last_name="Obi", email="a@example.com", briefs=[])
    page = _client(settings).get("/").text
    assert '<section class="card" data-state="waiting">' in page and "Approve the launch memo" in page
    assert '<span class="count" aria-label="1 waiting">1</span>' in page
    assert "<strong>1</strong><small>Waiting for your answer</small>" in page


def test_a_running_strategist_shows_as_working(settings):
    client = _client(settings)
    client.app.state.engine.start_run("think", "General", {"question": "x"}, work=False)
    assert '<li data-state="working">' in client.get("/").text


def test_the_old_board_is_kept_as_overview(settings):
    client = _client(settings)
    assert 'href="/overview"' in client.get("/").text and "Top priorities" in client.get("/overview").text


def test_the_owner_look_leaves_the_public_pages_their_own_background():
    """A light background on every body once hid the public page's white headline (15 Nov 2026)."""
    import re

    from personal_agent.api import ui

    for selector in re.findall(r"([^{}]*)\{[^{}]*background:[^{}]*\}", ui.CSS):
        s = " ".join(selector.split())
        if re.search(r"(^|[\s>+~,])body(?![\w.\-:])", s):  # a rule on every body, not body.public
            assert s.endswith("body:not(.public)") or s == "body", s
