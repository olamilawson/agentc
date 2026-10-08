"""Owner approval node, shared by every workflow graph (PRD: approval gate).

The owner's approval is the only step that lets a graph move on to a release or
filing action. An approval matches the exact content by hash; if the content no
longer matches, the approval lapses and the owner approves again.
"""

from __future__ import annotations

from typing import Callable

from langgraph.types import interrupt

from personal_agent.db import Database
from personal_agent.gateway.tools import content_hash


def make_approval_node(db: Database, payload_of: Callable[[dict], dict]):
    """payload_of(state) returns the exact content the owner is asked to approve.

    It must be deterministic for a given state: the node re-runs from the top
    when the graph resumes, and the hash has to come out the same.
    """

    def approval(state: dict) -> dict:
        # One interrupt per node execution; a mismatched or lapsed approval
        # routes back into this node (a fresh interrupt) so the owner can
        # approve again — the graph loop is bounded by human decisions.
        payload = payload_of(state)
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

    return approval
