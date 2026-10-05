"""Release gate: no release without approval matching exact content by hash."""

from __future__ import annotations

import pytest

from personal_agent.db import Database
from personal_agent.gateway.tools import (
    ToolClass,
    ToolGateway,
    ToolRefused,
    ToolSpec,
    content_hash,
)
from tests.conftest import INPUTS, draft_json, make_engine


def test_release_tool_refuses_unapproved_content(settings):
    db = Database(settings.database_url)
    gw = ToolGateway(db)
    gw.register(ToolSpec("send_test_message", ToolClass.RELEASE, fn=lambda p: {"sent": True}))

    with pytest.raises(Exception):  # no approval recorded at all
        gw.call("run-1", "release", ["send_test_message"], "send_test_message", {"subject": "s", "body": "b"})


def test_approval_node_rejects_mismatched_hash_and_asks_again(settings):
    """Content changed after approval -> the approval lapses and the owner approves again."""
    engine = make_engine(settings, [draft_json()])
    run_id = engine.start_run("foundation-test", "acme", INPUTS)
    assert engine.db.get_run(run_id)["status"] == "waiting_for_approval"

    # An approval recorded against the wrong content (as if the draft had changed
    # elsewhere after the owner signed off) must not release anything.
    db = engine.db
    stale_id = db.record_approval(run_id, "0" * 64, "approved", None)

    from langgraph.types import Command

    engine._invoke(run_id, Command(resume={"approval_id": stale_id}), None)

    run = db.get_run(run_id)
    assert run["status"] == "waiting_for_approval"  # re-interrupted, not released
    assert "approve again" in engine.queue()[0]["error"]

    # The correct approval now completes the run.
    done = engine.approve(run_id, "approved", None)
    assert done["status"] == "done"
    assert done["outputs"]["released"]["sent"] is True


def test_release_requires_matching_hash_even_with_an_approval_in_history(settings):
    db = Database(settings.database_url)
    gw = ToolGateway(db)
    gw.register(ToolSpec("send_test_message", ToolClass.RELEASE, fn=lambda p: {"sent": True}))

    approved_payload = {"subject": "s", "body": "b"}
    db.record_approval("run-2", content_hash(approved_payload), "approved", None)

    # Same content -> executes once; the identical call replays via idempotency.
    assert gw.call("run-2", "release", ["send_test_message"], "send_test_message", approved_payload) == {"sent": True}
    assert gw.call("run-2", "release", ["send_test_message"], "send_test_message", approved_payload) == {"sent": True}

    # Different content despite an existing approval -> held for a fresh approval.
    from personal_agent.gateway.tools import ApprovalRequired

    with pytest.raises(ApprovalRequired):
        gw.call("run-2", "release", ["send_test_message"], "send_test_message", {"subject": "s", "body": "CHANGED"})

    from sqlalchemy import select

    with db.engine.connect() as cx:
        from personal_agent.db import tool_calls

        rows = [dict(r) for r in cx.execute(select(tool_calls)).mappings()]
    outcomes = sorted(r["outcome"] for r in rows)
    assert outcomes == ["awaiting_approval", "executed", "replay"]


def test_allowlist_refusal_is_logged(settings):
    db = Database(settings.database_url)
    gw = ToolGateway(db)
    gw.register(ToolSpec("read_brief", ToolClass.READ, fn=lambda p: {"ok": True}))

    with pytest.raises(ToolRefused):
        gw.call("run-3", "draft", allowed_tools=[], name="read_brief", payload={})

    from sqlalchemy import select

    with db.engine.connect() as cx:
        from personal_agent.db import audit_log, tool_calls

        calls = [dict(r) for r in cx.execute(select(tool_calls)).mappings()]
        audits = [dict(r) for r in cx.execute(select(audit_log)).mappings()]
    assert calls[0]["outcome"] == "refused"
    assert any(a["action"] == "tool_refused" for a in audits)
