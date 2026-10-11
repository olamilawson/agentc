"""The weekly learning review: evidence, proposals as diffs, and the owner's decision."""

from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient

from personal_agent import learning
from personal_agent.api.main import create_app
from personal_agent.db import Database
from personal_agent.engine.runner import Engine
from personal_agent.harness.models import RecordedGateway
from personal_agent.skills.loader import load_library
from tests.test_strategy_drafting import CLEAN, GOAL, OUTLINE, SOURCE, body

SKILL = "# Strategy drafting\n\n## Writing standard\n\n- Lead with the point.\n- One idea per paragraph.\n"
RULE = "- Lead with the point."
BETTER = "- Lead with the point, in one sentence of fewer than twenty words."


def proposal(run_id: str, **changes) -> dict:
    out = {
        "rationale": "The owner shortened the opening sentence of every section.",
        "evidence_run_ids": [run_id], "find": RULE, "replace": BETTER,
    }
    out.update(changes)
    return out


class Recording(RecordedGateway):
    def __init__(self, responses):
        super().__init__(responses)
        self.contexts: dict[str, list[str]] = {}

    def complete(self, contract, context, images=None):
        self.contexts.setdefault(contract.node, []).append(context)
        return super().complete(contract, context, images)


def _engine(settings, proposals=None) -> tuple[Engine, Recording]:
    skill = settings.skills_dir / "strategy-drafting" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(SKILL, encoding="utf-8", newline="\n")
    gateway = Recording({
        "questions": [{"questions": []}] * 3, "outline": [OUTLINE] * 3,
        "draft_section": [body("A long and winding first sentence that takes its time."), body("Win on speed.")] * 3,
        "critique": [CLEAN] * 6, "revise": [body("Shorter."), body("Win on speed.")] * 3,
        "propose_skill_changes": proposals or [],
    })
    engine = Engine(settings, Database(settings.database_url), gateway=gateway)
    acme = engine.harness.clients.ensure("acme")
    engine.harness.clients.store_file(acme, "research.md", SOURCE.encode())
    return engine, gateway


def _edited_run(engine: Engine) -> str:
    """A drafting run the owner edited at the end; returns its run id."""
    run_id = engine.start_run("strategy-drafting", "acme", {"goal": GOAL, "source_paths": ["research.md"]})
    engine.approve(run_id, "approved", None)  # the outline, as shown
    draft = engine.queue()[0]["content"]["body"]
    engine.approve(run_id, "approved", None, edited_body=draft.replace("A long and winding first sentence that takes its time.", "SMEs are leaving."))
    return run_id


def _skill_text(settings) -> str:
    return (settings.skills_dir / "strategy-drafting" / "SKILL.md").read_text(encoding="utf-8")


def test_a_week_with_no_corrections_proposes_nothing_and_calls_no_model(settings):
    engine, gateway = _engine(settings)
    run = learning.run_review(engine)
    assert run["status"] == "done" and run["workflow"] == "learning-review"
    assert run["outputs"]["proposals"] == [] and run["tokens_in"] == 0
    assert "propose_skill_changes" not in gateway.contexts


def test_owner_edits_become_a_proposed_diff_and_nothing_changes_until_accepted(settings):
    engine, gateway = _engine(settings, proposals=[{"proposals": []}])
    run_id = _edited_run(engine)
    gateway._responses["propose_skill_changes"] = [{"proposals": [proposal(run_id)]}]
    before_version, _ = load_library(settings.skills_dir)

    review = learning.run_review(engine)
    assert review["status"] == "done" and review["outputs"]["workflows_reviewed"] == ["strategy-drafting"]
    # The model saw the skill file and the owner's edit as marked data, with its run id.
    context = gateway.contexts["propose_skill_changes"][0]
    assert RULE in context and f'<untrusted_data source="owner edit, run {run_id}, final">' in context
    assert "+SMEs are leaving." in context

    (pending,) = engine.db.list_proposals(status="pending")
    assert pending["skill"] == "strategy-drafting" and pending["evidence"] == [run_id]
    assert _skill_text(settings) == SKILL  # proposing changes nothing
    assert load_library(settings.skills_dir)[0] == before_version

    accepted = learning.decide(engine, pending["id"], "accepted")
    assert accepted["status"] == "accepted"
    assert BETTER in _skill_text(settings) and RULE + "\n" not in _skill_text(settings)
    assert accepted["library_version"] == load_library(settings.skills_dir)[0] != before_version
    assert BETTER in engine.harness.skills["strategy-drafting"]  # the next run uses the new file
    # Outside a git repository the change is still made, and the record says it was not committed.
    assert accepted["commit"] is None and "not in a git repository" in accepted["decision_note"]


def test_declining_changes_nothing(settings):
    engine, gateway = _engine(settings)
    run_id = _edited_run(engine)
    gateway._responses["propose_skill_changes"] = [{"proposals": [proposal(run_id)]}]
    learning.run_review(engine)
    (pending,) = engine.db.list_proposals(status="pending")
    declined = learning.decide(engine, pending["id"], "declined", "one document is not a pattern")
    assert declined["status"] == "declined" and _skill_text(settings) == SKILL
    with pytest.raises(ValueError, match="already declined"):
        learning.decide(engine, pending["id"], "accepted")


def test_proposals_that_cannot_stand_are_discarded_with_the_reason(settings):
    engine, gateway = _engine(settings)
    run_id = _edited_run(engine)
    gateway._responses["propose_skill_changes"] = [{"proposals": [
        proposal(run_id, find="- A rule that is not in the file."),
        proposal(run_id, replace="- Lead with the point, as the Acme launch plan did."),
        proposal("not-a-run"),
        proposal(run_id, find="", replace="## Examples\n\n- Open with the conclusion."),
    ]}]
    review = learning.run_review(engine)
    reasons = [d["reason"] for d in review["outputs"]["discarded"]]
    assert "nowhere in the skill file" in reasons[0]
    assert "names a client (acme)" in reasons[1]
    assert "not in the evidence" in reasons[2]
    (kept,) = engine.db.list_proposals(status="pending")  # the append is valid
    assert kept["find"] == ""
    learning.decide(engine, kept["id"], "accepted")
    assert _skill_text(settings).endswith("## Examples\n\n- Open with the conclusion.\n")


def test_a_proposal_overtaken_by_another_is_set_aside_not_forced(settings):
    engine, gateway = _engine(settings)
    run_id = _edited_run(engine)
    gateway._responses["propose_skill_changes"] = [{"proposals": [
        proposal(run_id), proposal(run_id, replace="- Lead with the recommendation."),
    ]}]
    learning.run_review(engine)
    pending = engine.db.list_proposals(status="pending")
    first = next(p for p in pending if p["replace"] == BETTER)
    second = next(p for p in pending if p["replace"] != BETTER)
    learning.decide(engine, first["id"], "accepted")
    with pytest.raises(ValueError, match="no longer applies"):
        learning.decide(engine, second["id"], "accepted")
    assert engine.db.get_proposal(second["id"])["status"] == "stale"
    assert BETTER in _skill_text(settings)


def test_accepted_change_is_committed_when_the_library_is_in_git(settings):
    engine, gateway = _engine(settings)

    def git(*args):
        return subprocess.run(["git", "-C", str(settings.skills_dir), *args], capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "owner@example.org")
    git("config", "user.name", "Owner")
    git("add", "-A")
    git("commit", "-q", "-m", "library")
    (settings.skills_dir / "unrelated.txt").write_text("not part of the change", encoding="utf-8")

    run_id = _edited_run(engine)
    gateway._responses["propose_skill_changes"] = [{"proposals": [proposal(run_id)]}]
    learning.run_review(engine)
    (pending,) = engine.db.list_proposals(status="pending")
    accepted = learning.decide(engine, pending["id"], "accepted")
    assert accepted["commit"] and accepted["commit"] == git("rev-parse", "--short", "HEAD").stdout.strip()
    # Only the skill file is in the commit; other work in the tree is left alone.
    assert git("show", "--name-only", "--format=", "HEAD").stdout.split() == ["strategy-drafting/SKILL.md"]
    assert "unrelated.txt" in git("status", "--short").stdout


def test_weekly_report_gives_edit_and_rejection_rates_by_workflow(settings):
    engine, _ = _engine(settings)
    _edited_run(engine)  # two approvals (outline, final), one of them edited
    rejected = engine.start_run("strategy-drafting", "acme", {"goal": GOAL, "source_paths": ["research.md"]})
    engine.approve(rejected, "rejected", "wrong document type")
    report = learning.run_review(engine)["outputs"]["report"]["strategy-drafting"]
    assert (report["runs"], report["approved"], report["edited"], report["rejected"]) == (2, 2, 1, 1)
    assert report["edit_rate"] == 0.5 and report["rejection_rate"] == 0.333
    assert "learning-review" not in learning.weekly_report(engine.db, "0", "9")


def test_library_page_shows_the_diff_and_takes_the_owners_decision(settings):
    skill = settings.skills_dir / "strategy-drafting" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(SKILL, encoding="utf-8", newline="\n")
    client = TestClient(create_app(settings, gateway=RecordedGateway({})))
    db = Database(settings.database_url)
    proposal_id = db.create_proposal("review-1", "strategy-drafting", "Shorter openings.", ["run-1"], RULE, BETTER)

    page = client.get("/library").text
    assert "-- Lead with the point." in page and "+- Lead with the point, in one sentence" in page
    assert client.get("/learning/proposals").json()[0]["diff"].startswith("--- SKILL.md (current)")
    assert client.post(f"/learning/proposals/{proposal_id}", data={"decision": "accepted"}).json()["status"] == "accepted"
    assert BETTER in client.get("/skills").json()["skills"]["strategy-drafting"]
    assert client.post(f"/learning/proposals/{proposal_id}", data={"decision": "declined"}).status_code == 409
    assert client.post("/learning/reviews").json()["status"] == "done"
    assert "Weekly report" in client.get("/library").text
