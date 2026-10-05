"""Foundation gate: a test run pauses for approval and resumes after a restart."""

from __future__ import annotations

from tests.conftest import INPUTS, VALID_DRAFT, draft_json, make_client


def test_pause_resume_flow(settings):
    # Process one: start a run; it must stop at the approval queue.
    c1 = make_client(settings, [draft_json()])
    created = c1.post(
        "/runs",
        json={"workflow": "foundation-test", "client": "acme", "inputs": INPUTS},
    )
    assert created.status_code == 201
    run_id = created.json()["run_id"]

    assert c1.get(f"/runs/{run_id}").json()["status"] == "waiting_for_approval"

    pending = c1.get("/approvals/pending").json()
    assert len(pending) == 1
    assert pending[0]["run_id"] == run_id
    assert pending[0]["content"]["subject"] == VALID_DRAFT["message_subject"]
    assert len(pending[0]["content_hash"]) == 64

    # Process two (after a restart): a fresh engine on the same databases approves.
    c2 = make_client(settings, [])
    approved = c2.post(f"/runs/{run_id}/approval", json={"decision": "approved"})
    assert approved.status_code == 200
    run = approved.json()
    assert run["status"] == "done"
    assert run["outputs"]["released"]["sent"] is True
    assert run["tokens_in"] > 0 and run["cost_usd"] > 0

    # The release executed exactly once, under the gateway's idempotency key.
    from personal_agent.db import Database

    with Database(settings.database_url).engine.connect() as cx:
        from sqlalchemy import select
        from personal_agent.db import tool_calls

        rows = [
            dict(r)
            for r in cx.execute(
                select(tool_calls).where(tool_calls.c.run_id == run_id)
            ).mappings()
        ]
    executed = [r for r in rows if r["outcome"] == "executed"]
    assert len(executed) == 1
    assert executed[0]["idempotency_key"]


def test_queue_page_shows_exact_content_before_any_release(settings):
    c = make_client(settings, [draft_json()])
    run_id = c.post(
        "/runs",
        json={"workflow": "foundation-test", "client": "acme", "inputs": INPUTS},
    ).json()["run_id"]

    html = c.get("/queue").text
    assert VALID_DRAFT["message_subject"] in html
    assert VALID_DRAFT["message_body"] in html
    assert c.get(f"/runs/{run_id}").json()["status"] == "waiting_for_approval"
