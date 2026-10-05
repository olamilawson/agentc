"""The Foundation test graph (PRD: workflow graphs / delivery phase one).

A deliberately small workflow that exercises every Foundation feature:

    intake (code) -> draft (model, standard tier) -> owner approval (interrupt)
        -> release (release-class tool via the gateway) -> finalize

The approval node is the only route to a release action. The release tool here
is an in-memory stub standing in for the future Microsoft Graph send tool, so
the Foundation gate — "a test run pauses for approval and resumes after a
restart" — can be demonstrated without any provider access.
"""

from __future__ import annotations

from typing import Annotated, TypedDict

import operator
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from personal_agent.gateway.tools import ApprovalLapsed, ApprovalRequired, content_hash
from personal_agent.harness.budgets import BudgetTracker
from personal_agent.harness.context import build_context

ROLE = (
    "You are the owner's marketing agent. You prepare work; the owner approves "
    "everything that leaves the system. Concise, intelligent, human, senior, "
    "non-cheesy and commercially grounded."
)


class DraftIn(BaseModel):
    objective: str = Field(min_length=1)
    context_notes: str = ""


class DraftOut(BaseModel):
    message_subject: str = Field(min_length=1, max_length=200)
    message_body: str = Field(min_length=1)
    questions: list[str] = []


class TestGraphState(TypedDict, total=False):
    run_id: str
    client: str
    inputs: dict
    usage: dict
    flags: list[str]
    draft_subject: str
    draft_body: str
    questions: list[str]
    approval_error: str | None
    approved_hash: str
    released: dict
    error: str | None


def build_test_graph(h):
    """h: engine.runner.Harness — kept untyped here to avoid an import cycle."""
    db = h.db

    def intake(state: TestGraphState) -> dict:
        # Code node: no model. Uploaded/received material is marked as data and
        # instruction-like text inside it is ignored and flagged.
        brief_text = (state["inputs"] or {}).get("context_notes", "")
        _, flags = build_context(
            ROLE, h.skill_text, [], [("brief", brief_text)], {"client": state["client"]}
        )
        return {"flags": state.get("flags", []) + flags}

    def draft(state: TestGraphState) -> dict:
        tracker = BudgetTracker(h.settings.budgets)
        prior = state.get("usage") or {}
        tracker.add_usage(prior.get("tokens_in", 0), prior.get("tokens_out", 0), prior.get("cost_usd", 0.0))
        _, context = build_context(
            ROLE,
            h.skill_text,
            [],
            [("brief", (state["inputs"] or {}).get("context_notes", ""))],
            {"client": state["client"], "objective": (state["inputs"] or {}).get("objective", "")},
        )
        out = h.node_runner(tracker).run(
            h.contracts["draft"],
            {
                "objective": (state["inputs"] or {}).get("objective", ""),
                "context_notes": (state["inputs"] or {}).get("context_notes", ""),
            },
            context=context,
        )
        return {
            "draft_subject": out["message_subject"],
            "draft_body": out["message_body"],
            "questions": out.get("questions", []),
            "usage": {
                "tokens_in": tracker.tokens_in,
                "tokens_out": tracker.tokens_out,
                "cost_usd": tracker.cost_usd,
            },
            "fallback_used": getattr(h.gateway, "used_fallback", False),
        }

    def approval(state: TestGraphState) -> dict:
        # The owner's approval is the only step that releases a reply.
        # One interrupt per node execution; a mismatched or lapsed approval
        # routes back into this node (a fresh interrupt) so the owner can
        # approve again — the graph loop is bounded by human decisions.
        payload = {"subject": state["draft_subject"], "body": state["draft_body"]}
        p_hash = content_hash(payload)
        resume = interrupt(
            {
                "type": "approval",
                "content": payload,
                "content_hash": p_hash,
                "error": state.get("approval_error"),
            }
        )
        rec = db.get_approval(resume["approval_id"])
        if (
            rec
            and rec["decision"] == "approved"
            and not rec["lapsed"]
            and rec["content_hash"] == p_hash
        ):
            return {"approval_error": None, "approved_hash": p_hash}
        if rec and rec["content_hash"] != p_hash:
            # Content changed after approval: the approval lapses.
            db.lapse_approval(rec["id"])
            db.audit(state["run_id"], "approval_lapsed", {
                "approval_id": rec["id"],
                "note": "content no longer matches the approved hash; owner must approve again",
            })
        return {"approval_error": "approval does not match the current content; approve again"}

    def after_approval(state: TestGraphState) -> str:
        return "approval" if state.get("approval_error") else "release"

    def release(state: TestGraphState) -> dict:
        try:
            result = h.tools.call(
                state["run_id"],
                "release",
                h.contracts["release"].allowed_tools,
                "send_test_message",
                {"subject": state["draft_subject"], "body": state["draft_body"]},
            )
        except (ApprovalRequired, ApprovalLapsed) as e:
            return {"approval_error": str(e)}
        return {"released": result, "approval_error": None}

    def finalize(state: TestGraphState) -> dict:
        return {"error": None}

    def after_release(state: TestGraphState) -> str:
        return "approval" if state.get("approval_error") else "finalize"

    builder = StateGraph(TestGraphState)
    builder.add_node("intake", intake)
    builder.add_node("draft", draft)
    builder.add_node("approval", approval)
    builder.add_node("release", release)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "intake")
    builder.add_edge("intake", "draft")
    builder.add_edge("draft", "approval")
    builder.add_conditional_edges(
        "approval", after_approval, {"approval": "approval", "release": "release"}
    )
    builder.add_conditional_edges("release", after_release, {"approval": "approval", "finalize": "finalize"})
    builder.add_edge("finalize", END)
    return builder
