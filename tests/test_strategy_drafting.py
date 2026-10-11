"""Strategy drafting: questions, the two owner pauses, the critique loop, edits and send-backs."""

from __future__ import annotations

from fastapi.testclient import TestClient

from personal_agent.api.main import create_app
from personal_agent.db import Database
from personal_agent.engine.runner import Engine
from personal_agent.engine.strategy_drafting import (
    edit_rate_by_section,
    outline_text,
    parse_outline,
    unsupported_figures,
)
from personal_agent.harness.models import RecordedGateway

GOAL = "A go-to-market strategy for the SME account, budget 60m naira, launch in Q2."
SOURCE = "Research note: 41 percent of Lagos SMEs bank with a fintech wallet. Budget approved: 60m naira."
OUTLINE = {
    "title": "SME account: go-to-market strategy",
    "sections": [
        {"heading": "The problem", "purpose": "what is at stake commercially"},
        {"heading": "The strategy", "purpose": "the single choice being made"},
    ],
}
NO_QUESTIONS = {"questions": []}
CLEAN = {"faults": []}


def body(text: str) -> dict:
    return {"body": text}


def _engine(settings, **responses) -> Engine:
    responses.setdefault("questions", [NO_QUESTIONS])
    responses.setdefault("outline", [OUTLINE])
    engine = Engine(settings, Database(settings.database_url), gateway=RecordedGateway(responses))
    acme = engine.harness.clients.ensure("acme")
    engine.harness.clients.store_file(acme, "research.md", SOURCE.encode())
    return engine


def _start(engine: Engine, **changes) -> dict:
    inputs = {"goal": GOAL, "document_type": "strategy", "source_paths": ["research.md"], **changes}
    return engine.db.get_run(engine.start_run("strategy-drafting", "acme", inputs))


def _to_final(engine: Engine, run: dict) -> dict:
    """Approve the outline as shown; returns the run at its next pause."""
    return engine.approve(run["id"], "approved", None)


# --- the two pauses -------------------------------------------------------------------
def test_run_pauses_at_the_outline_then_at_the_end_and_saves_what_was_approved(settings):
    engine = _engine(
        settings,
        draft_section=[body("SMEs are leaving for wallets: 41 percent already have."), body("Win on speed of opening.")],
        critique=[CLEAN],
    )
    run = _start(engine)
    assert run["status"] == "waiting_for_approval"
    outline = engine.queue()[0]
    assert outline["action"] == "outline" and outline["editable"]
    assert outline["content"]["body"] == "1. The problem: what is at stake commercially\n2. The strategy: the single choice being made\n"
    assert run["outputs"]["sections_drafted"] == 0  # nothing is drafted before the outline is approved

    run = _to_final(engine, run)
    assert run["status"] == "waiting_for_approval"
    final = engine.queue()[0]
    assert final["action"] == "save"
    assert final["content"]["body"].startswith("# SME account: go-to-market strategy\n\n## The problem\n\nSMEs are leaving")
    acme = engine.harness.clients.scope("acme")
    assert [f["path"] for f in engine.harness.clients.list_files(acme)] == ["research.md"]

    done = engine.approve(run["id"], "approved", None)
    assert done["status"] == "done"
    saved = done["outputs"]["filed"]["file"]
    assert saved.startswith("sme-account-go-to-market-strategy-")
    assert engine.harness.clients.read_bytes(acme, saved).decode() == final["content"]["body"]
    assert done["outputs"]["edit_rate"] == {"The problem": 0.0, "The strategy": 0.0}
    assert engine.db.list_owner_edits(run["id"]) == []


def test_missing_information_asks_up_to_five_questions_and_waits_for_input(settings):
    engine = _engine(settings, questions=[{"questions": ["What is the launch date?", "Who signs off the budget?"]}])
    run = _start(engine)
    assert run["status"] == "waiting_for_input"
    assert engine.queue() == []
    ask = engine.input_requests()[0]
    assert ask["questions"] == ["What is the launch date?", "Who signs off the budget?"]

    run = engine.provide_input(run["id"], "1 April. The MD.")
    assert run["status"] == "waiting_for_approval"
    assert run["outputs"]["answers"] == "1 April. The MD."
    assert engine.queue()[0]["action"] == "outline"


def test_owner_can_edit_the_outline_and_the_draft_follows_the_edited_sections(settings):
    engine = _engine(
        settings, draft_section=[body("One."), body("Two."), body("Three.")], critique=[CLEAN],
    )
    run = _start(engine)
    edited = "1. The problem: what is at stake\n2. The audience\n3. The strategy: one choice\n"
    run = engine.approve(run["id"], "approved", None, edited_body=edited)
    assert [s["heading"] for s in run["outputs"]["sections"]] == ["The problem", "The audience", "The strategy"]
    assert run["outputs"]["sections_drafted"] == 3
    edits = engine.db.list_owner_edits(run["id"])
    assert len(edits) == 1 and edits[0]["stage"] == "outline" and edits[0]["after"] == edited


def test_outline_sent_back_with_a_comment_is_redone_before_any_drafting(settings):
    second = {**OUTLINE, "title": "SME account: strategy, shorter"}
    engine = _engine(settings, outline=[OUTLINE, second])
    run = _start(engine)
    run = engine.approve(run["id"], "sent_back", "lead with the audience")
    assert run["status"] == "waiting_for_approval"
    assert engine.queue()[0]["content"]["subject"] == "Outline: SME account: strategy, shorter"
    assert run["outputs"]["sections_drafted"] == 0


def test_owner_edits_at_the_end_are_saved_and_the_edit_rate_recorded(settings):
    engine = _engine(settings, draft_section=[body("First version."), body("Win on speed.")], critique=[CLEAN])
    run = _to_final(engine, _start(engine))
    draft = engine.queue()[0]["content"]["body"]
    edited = draft.replace("First version.", "SMEs are leaving for wallets. That is the problem.")
    done = engine.approve(run["id"], "approved", None, edited_body=edited)
    assert done["status"] == "done"
    acme = engine.harness.clients.scope("acme")
    assert engine.harness.clients.read_bytes(acme, done["outputs"]["filed"]["file"]).decode() == edited
    rate = done["outputs"]["edit_rate"]
    assert rate["The problem"] > 0.5 and rate["The strategy"] == 0.0
    edit = engine.db.list_owner_edits(run["id"])[0]
    assert (edit["stage"], edit["before"], edit["after"]) == ("final", draft, edited)


def test_final_draft_sent_back_starts_a_new_pass_with_the_owners_comment(settings):
    engine = _engine(
        settings,
        draft_section=[body("Long first."), body("Long second.")],
        critique=[CLEAN, CLEAN],
        revise=[body("Short first."), body("Short second.")],
    )
    run = _to_final(engine, _start(engine))
    run = engine.approve(run["id"], "sent_back", "half the length")
    assert run["status"] == "waiting_for_approval"
    final = engine.queue()[0]["content"]["body"]
    assert "Short first." in final and "Short second." in final and "Long" not in final


# --- the critique loop ------------------------------------------------------------------
def test_critic_faults_are_revised_section_by_section(settings):
    engine = _engine(
        settings,
        draft_section=[body("In today's fast-paced world, SMEs matter."), body("Win on speed.")],
        critique=[{"faults": [{"section": 1, "fault": "opens with a cliche"}]}, CLEAN],
        revise=[body("SMEs are leaving for wallets.")],  # one response: only the faulted section is rewritten
    )
    run = _to_final(engine, _start(engine))
    assert run["outputs"]["revisions"] == 1 and run["outputs"]["faults"] == []
    assert "fast-paced" not in run["outputs"]["document"] and "Win on speed." in run["outputs"]["document"]


def test_critique_and_revision_run_twice_at_most_then_the_open_faults_go_to_the_owner(settings):
    fault = {"faults": [{"section": 2, "fault": "hedged into nothing"}]}
    # Three critiques and two revisions are recorded: a third revision would fail the run.
    engine = _engine(
        settings, draft_section=[body("One."), body("Maybe.")], critique=[fault] * 3, revise=[body("Perhaps.")] * 2,
    )
    run = _to_final(engine, _start(engine))
    assert run["status"] == "waiting_for_approval" and run["outputs"]["revisions"] == 2
    item = engine.queue()[0]
    assert "Open fault, The strategy: hedged into nothing" in item["notes"]
    assert "Open fault" not in item["content"]["body"]


def test_a_figure_found_in_no_source_is_a_fault_without_asking_the_critic(settings):
    engine = _engine(
        settings,
        draft_section=[body("Wallets hold 41 percent of SMEs."), body("We will win 250,000 accounts.")],
        critique=[CLEAN, CLEAN],
        revise=[body("We will win accounts on speed of opening.")],
    )
    run = _to_final(engine, _start(engine))
    assert run["outputs"]["revisions"] == 1 and run["outputs"]["faults"] == []
    assert "250,000" not in run["outputs"]["document"]


# --- sources ----------------------------------------------------------------------------
def test_sources_come_only_from_the_runs_own_client_folder(settings):
    engine = _engine(settings)
    beta = engine.harness.clients.ensure("beta")
    engine.harness.clients.store_file(beta, "plan.md", b"Beta's confidential launch plan.")
    run = _start(engine, source_paths=["../beta/plan.md"])
    assert run["status"] == "failed" and "outside client" in run["error_reason"]


def test_with_no_sources_named_the_folder_is_read_and_unreadable_files_skipped(settings):
    engine = _engine(settings)
    acme = engine.harness.clients.scope("acme")
    engine.harness.clients.store_file(acme, "logo.png", b"not text")
    run = _start(engine, source_paths=[])
    assert run["outputs"]["sources"] == ["research.md"]
    assert any("skipped logo.png" in flag for flag in run["flags"])


# --- helpers ----------------------------------------------------------------------------
def test_outline_round_trips_and_reads_an_owners_edits():
    sections = OUTLINE["sections"]
    assert parse_outline(outline_text(sections)) == sections
    assert parse_outline("- Why now\n\n* The ask: 60m naira\nRisks") == [
        {"heading": "Why now", "purpose": ""},
        {"heading": "The ask", "purpose": "60m naira"},
        {"heading": "Risks", "purpose": ""},
    ]


def test_edit_rate_is_per_section():
    draft = "# T\n\n## A\n\nsame text\n\n## B\n\nold words here\n"
    approved = "# T\n\n## A\n\nsame text\n\n## C\n\nnew section\n"
    assert edit_rate_by_section(draft, approved) == {"A": 0.0, "B": 1.0, "C": 1.0}


def test_figures_must_appear_in_the_evidence():
    assert unsupported_figures(["Budget is 60m, 41 percent.", "1. First step\n2. Second step"], SOURCE + GOAL) == []
    (fault,) = unsupported_figures(["We reach 250,000 people by 2031."], SOURCE + GOAL)
    assert fault["section"] == 1 and "2031" in fault["fault"] and "250000" in fault["fault"]


# --- API ----------------------------------------------------------------------------------
def test_api_takes_answers_and_an_edited_approval_and_other_workflows_refuse_edits(settings):
    gateway = RecordedGateway({
        "questions": [{"questions": ["What is the launch date?"]}], "outline": [OUTLINE],
        "draft_section": [body("One."), body("Two.")], "critique": [CLEAN],
        "draft": [{"message_subject": "s", "message_body": "b", "questions": []}],
    })
    client = TestClient(create_app(settings, gateway=gateway))
    run_id = client.post("/runs", json={
        "workflow": "strategy-drafting", "client": "acme", "inputs": {"goal": GOAL},
    }).json()["run_id"]
    assert client.get("/inputs/pending").json()[0]["questions"] == ["What is the launch date?"]
    assert "Questions before drafting" in client.get("/queue").text
    assert client.post(f"/runs/{run_id}/approval", json={"decision": "approved"}).status_code == 409

    answered = client.post(f"/runs/{run_id}/input", data={"answers": "1 April"})
    assert answered.json()["status"] == "waiting_for_approval"
    assert "Edit, then approve" in client.get("/queue").text
    # A browser posts the textarea with CRLF line ends; an unchanged outline is not an edit.
    unchanged = outline_text(OUTLINE["sections"]).replace("\n", "\r\n")
    drafted = client.post(f"/runs/{run_id}/approval", data={"decision": "approved", "edited_body": unchanged})
    assert drafted.json()["outputs"]["sections_drafted"] == 2
    assert client.get(f"/runs/{run_id}/edits").json() == []

    other = client.post("/runs", json={
        "workflow": "foundation-test", "client": "acme", "inputs": {"objective": "x"},
    }).json()["run_id"]
    refused = client.post(f"/runs/{other}/approval", json={"decision": "approved", "edited_body": "changed"})
    assert refused.status_code == 409
