"""Creative review: page rendering, images to the model, link checks, approval before filing."""

from __future__ import annotations

import copy
import io
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from personal_agent.db import Database, tool_calls
from personal_agent.engine.creative_review import CRITERIA, calibration_examples, combine_groups, unlinked
from personal_agent.engine.runner import Engine
from personal_agent.evaluation import evaluate, format_report, load_set, text_pdf
from personal_agent.harness.models import RecordedGateway
from personal_agent.rendering import UnsupportedWork, render_file

BRIEF = "Outdoor for the SME account. Objective: account openings. Audience: shop owners. Tone: plain."
WORK = ["Open your business account in ten minutes.", "Bring your CAC number. Leave with an account."]


def ratings(score: int = 4, page: int | None = 1, area: str | None = "headline") -> dict:
    cite = [{"page": page, "area": area, "observation": "One clear line."}] if page else []
    return {"ratings": [{"criterion": c, "score": score, "citations": copy.deepcopy(cite)} for c in CRITERIA]}


def feedback(**changes) -> dict:
    request = {"priority": 1, "request": "Cut the second line.", "criterion": "craft", "page": 2, "area": "body copy"}
    request.update(changes)
    return {"summary": "Clear and on brief; one loose line.", "requests": [request]}


class Seeing(RecordedGateway):
    """A recorded gateway that also notes the page images each call carried."""

    def __init__(self, responses):
        super().__init__(responses)
        self.images: list[list[str]] = []

    def complete(self, contract, context, images=None):
        self.images.append([label for label, _ in images or []])
        return super().complete(contract, context, images)


def _engine(settings, pages=WORK, **responses) -> tuple[Engine, Seeing]:
    gateway = Seeing(responses)
    engine = Engine(settings, Database(settings.database_url), gateway=gateway)
    acme = engine.harness.clients.ensure("acme")
    engine.harness.clients.store_file(acme, "poster.pdf", text_pdf(pages))
    return engine, gateway


def _start(engine: Engine, **changes) -> dict:
    inputs = {"work_paths": ["poster.pdf"], "brief_text": BRIEF, "discipline": "copywriter", "work_kind": "outdoor"}
    return engine.db.get_run(engine.start_run("creative-review", "acme", {**inputs, **changes}))


# --- rendering (no model) -----------------------------------------------------------
def test_pdf_pages_render_to_images_and_text_at_the_configured_size():
    from PIL import Image

    pages = render_file(text_pdf(WORK), "poster.pdf", max_edge=400)
    assert [p.number for p in pages] == [1, 2]
    assert "ten minutes" in pages[0].text and "CAC number" in pages[1].text
    image = Image.open(io.BytesIO(pages[0].image))
    assert image.format == "JPEG" and max(image.size) == 400


def test_image_files_are_one_page_and_never_enlarged():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (300, 200), "navy").save(buf, format="PNG")
    (page,) = render_file(buf.getvalue(), "banner.png", max_edge=1024, first_number=7)
    assert page.number == 7 and page.text == ""
    assert Image.open(io.BytesIO(page.image)).size == (300, 200)


def test_word_files_give_text_only_and_decks_must_be_exported():
    from docx import Document

    doc, buf = Document(), io.BytesIO()
    doc.add_paragraph("Headline option one")
    doc.save(buf)
    (page,) = render_file(buf.getvalue(), "copy.docx", max_edge=1024)
    assert page.image is None and "Headline option one" in page.text
    with pytest.raises(UnsupportedWork, match="Export the deck as a PDF"):
        render_file(b"", "deck.pptx", max_edge=1024)


def test_gateway_sends_each_page_image_after_its_label(settings, monkeypatch):
    import litellm
    from types import SimpleNamespace

    from personal_agent.engine.creative_review import creative_contracts
    from personal_agent.harness.models import ModelGateway

    sent = {}

    def fake_completion(**kwargs):
        sent.update(kwargs)
        message = SimpleNamespace(content=json.dumps(ratings()))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

    monkeypatch.setattr(litellm, "completion", fake_completion)
    settings.route_table["strong"] = settings.route_table["standard"]
    out = ModelGateway(settings.route_table).complete(
        creative_contracts()["score_by_brief"], "context", images=[("Page 1 (poster.pdf)", b"\xff\xd8jpeg")]
    )
    assert len(out["data"]["ratings"]) == 5
    parts = sent["messages"][1]["content"]
    assert [p["type"] for p in parts] == ["text", "text", "image_url"]
    assert parts[1]["text"] == "Page 1 (poster.pdf)"
    assert parts[2]["image_url"]["url"].startswith("data:image/jpeg;base64,")


# --- the graph ------------------------------------------------------------------------
def test_work_is_scored_from_its_page_images_held_then_filed(settings):
    engine, gateway = _engine(settings, score_by_brief=[ratings()], write_feedback=[feedback()])
    run = _start(engine)
    assert run["status"] == "waiting_for_approval"
    # The scoring call saw both pages; the feedback call needs none.
    assert gateway.images == [["Page 1 (poster.pdf)", "Page 2 (poster.pdf)"], []]

    item = engine.queue()[0]
    assert "[Craft, page 2, body copy] Cut the second line." in item["content"]["body"]
    assert "Page 1, headline: One clear line." in item["content"]["body"]
    assert any("no calibration examples" in note for note in item["notes"])
    acme = engine.harness.clients.scope("acme")
    assert [f["path"] for f in engine.harness.clients.list_files(acme)] == ["poster.pdf"]  # nothing filed yet

    done = engine.approve(run["id"], "approved", None)
    assert done["status"] == "done"
    filed = done["outputs"]["filed"]["file"]
    assert engine.harness.clients.read_bytes(acme, filed).decode() == item["content"]["body"]

    with engine.db.engine.connect() as cx:
        calls = [dict(r) for r in cx.execute(select(tool_calls).where(tool_calls.c.run_id == run["id"])).mappings()]
    assert [(c["tool"], c["tool_class"]) for c in calls] == [("render_client_file", "read"), ("write_client_file", "draft")]
    # Image bytes stay out of the tool-call log: only the text and a has_image marker.
    assert sorted(calls[0]["detail"]["pages"][0]) == ["has_image", "number", "source", "text"]


def test_pages_are_scored_in_groups_and_combined(settings):
    settings.creative_review.pages_per_group = 2
    engine, gateway = _engine(
        settings, pages=WORK + ["Sterling SME Account."],
        score_by_brief=[ratings(5, page=1), ratings(2, page=3)], write_feedback=[feedback()],
    )
    run = _start(engine)
    assert [len(labels) for labels in gateway.images] == [2, 1, 0]
    first = run["outputs"]["ratings"][0]
    assert first["score"] == 4  # mean of 5 and 2, rounded half up
    assert [c["page"] for c in first["citations"]] == [1, 3]
    assert run["tokens_in"] == 150 * 3  # usage carries across the group calls


def test_rating_without_a_cited_page_returns_to_scoring(settings):
    engine, _ = _engine(
        settings, score_by_brief=[ratings(page=None), ratings()], write_feedback=[feedback(), feedback()]
    )
    run = _start(engine)
    assert run["status"] == "waiting_for_approval" and run["outputs"]["faults"] == []


def test_request_without_a_location_returns_to_feedback_only(settings):
    # One scoring response: an unlinked request must not send the work back to scoring.
    engine, _ = _engine(settings, score_by_brief=[ratings()], write_feedback=[feedback(area=None), feedback()])
    run = _start(engine)
    assert run["status"] == "waiting_for_approval" and run["outputs"]["faults"] == []


def test_link_check_returns_twice_at_most_then_shows_the_faults_beside_the_feedback(settings):
    engine, _ = _engine(settings, score_by_brief=[ratings()], write_feedback=[feedback(criterion=None)] * 3)
    run = _start(engine)
    assert run["status"] == "waiting_for_approval"
    item = engine.queue()[0]
    assert any("Open fault: request 1 names no criterion" in note for note in item["notes"])
    assert "Open fault" not in item["content"]["body"]


def test_link_rules():
    good = ratings()["ratings"]
    assert unlinked(good, feedback()["requests"], page_count=2) == ([], [])
    rating_faults, request_faults = unlinked(
        ratings(page=9)["ratings"][:1] + ratings(area=" ")["ratings"][:1],
        [{"priority": 1, "request": "x", "criterion": "tone", "page": 3, "area": "headline"}],
        page_count=2,
    )
    assert "page 9, which the work does not have" in rating_faults[0]
    assert "without saying where" in rating_faults[1]
    assert request_faults == ["request 1 names no location (a page of the work and the area on it)"]
    assert combine_groups([ratings(4)["ratings"], ratings(3)["ratings"]])[0]["score"] == 4  # 3.5 rounds up


def test_work_in_another_clients_folder_cannot_be_reviewed(settings):
    engine, _ = _engine(settings)
    beta = engine.harness.clients.ensure("beta")
    engine.harness.clients.store_file(beta, "secret.pdf", text_pdf(["Beta's unreleased campaign line."]))
    run = _start(engine, work_paths=["../beta/secret.pdf"])
    assert run["status"] == "failed" and "outside client" in run["error_reason"]
    assert run["outputs"] is None


def test_review_needs_a_brief_and_a_supported_file(settings):
    engine, _ = _engine(settings)
    assert "found no brief" in _start(engine, brief_text="")["error_reason"]
    acme = engine.harness.clients.scope("acme")
    engine.harness.clients.store_file(acme, "deck.pptx", b"not a real deck")
    assert "Export the deck as a PDF" in _start(engine, work_paths=["deck.pptx"])["error_reason"]


def test_calibration_examples_come_from_the_skill_by_kind():
    skill = (
        "# Creative review\n\n## Calibration examples\n\n### kind: social\nA caption the owner scored 4.\n\n"
        "### kind: outdoor\nA poster the owner scored 2.\n\n## Something else\nnot an example\n"
    )
    assert calibration_examples(skill, "outdoor") == ["A poster the owner scored 2."]
    assert calibration_examples(skill, "radio") == []
    assert calibration_examples("no examples section", "outdoor") == []


# --- evaluation set and runner ----------------------------------------------------------
SET = Path(__file__).resolve().parents[1] / "evaluation" / "sets" / "creative-review.jsonl"


def test_evaluation_set_is_fifteen_items_scored_on_the_graphs_criteria():
    items = load_set(SET)
    assert len(items) >= 15
    for item in items:
        assert sorted((item["owner"] or item["provisional"])["scores"]) == sorted(CRITERIA), item["id"]
        assert item["discipline"] in ("designer", "copywriter") and item["pages"], item["id"]


def _eval(settings, tmp_path, n: int, feedbacks: list[dict], **kwargs) -> dict:
    items = [
        {"id": f"crea-{i:03d}", "synthetic": False, "discipline": "copywriter", "work_kind": "outdoor",
         "brief": BRIEF, "pages": WORK, "provisional": None, "owner": {"scores": {c: 5 for c in CRITERIA}}}
        for i in range(n)
    ]
    path = tmp_path / "set.jsonl"
    path.write_text("\n".join(json.dumps(i) for i in items), encoding="utf-8")
    gateway = RecordedGateway({"score_by_brief": [ratings(4)] * n, "write_feedback": feedbacks})
    return evaluate("creative-review", path, settings, gateway=gateway, **kwargs)


def test_evaluation_gate_passes_on_an_owner_scored_set_with_every_link_in_place(settings, tmp_path):
    report = _eval(settings, tmp_path, 15, [feedback()] * 15, min_agreement=0.9)
    s = report["summary"]
    assert (s["within_one"], s["exact"], s["unlinked"], s["failed"]) == (1.0, 0.0, 0, 0)
    assert s["by_criterion"]["craft"]["mean_diff"] == -1.0
    assert report["gate"]["passed"], format_report(report)


def test_unlinked_requests_block_the_gate(settings, tmp_path):
    # Item 0 never links its request: three attempts, then it goes to the owner with the fault open.
    report = _eval(settings, tmp_path, 15, [feedback(page=None)] * 3 + [feedback()] * 14, min_agreement=0.5)
    assert report["summary"]["unlinked"] == 1
    assert not report["gate"]["passed"]
    assert any("lack a cited page, criterion or location" in r for r in report["gate"]["blocked_by"])
