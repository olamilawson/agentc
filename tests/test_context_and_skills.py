"""Untrusted content handling and skill-library version pinning."""

from __future__ import annotations

from personal_agent.skills.loader import load_library
from tests.conftest import INJECTED_BRIEF, INPUTS, make_engine


def test_injected_instructions_are_flagged_not_obeyed(settings):
    engine = make_engine(settings, [{"message_subject": "s", "message_body": "b", "questions": []}])
    inputs = dict(INPUTS, context_notes=INJECTED_BRIEF)
    run_id = engine.start_run("foundation-test", "acme", inputs)
    run = engine.db.get_run(run_id)
    assert run["status"] in {"waiting_for_approval", "done"}
    assert any("overrides instructions" in flag for flag in run["flags"])


def test_skill_library_version_pins_run_records(settings, tmp_path):
    engine = make_engine(settings, [{"message_subject": "s", "message_body": "b", "questions": []}])
    run_id = engine.start_run("foundation-test", "acme", INPUTS)
    version_before = engine.db.get_run(run_id)["skill_library_version"]

    skill_file = settings.skills_dir / "foundation-test" / "SKILL.md"
    skill_file.write_text(skill_file.read_text() + "\n- Edit: never use exclamation marks.\n")

    engine2 = make_engine(settings, [{"message_subject": "s", "message_body": "b", "questions": []}])
    run_id2 = engine2.start_run("foundation-test", "acme", INPUTS)
    version_after = engine2.db.get_run(run_id2)["skill_library_version"]

    assert version_before != version_after
    assert len(version_before) == 64


def test_loader_lists_skills(settings):
    version, skills = load_library(settings.skills_dir)
    assert "foundation-test" in skills
    assert "concise, senior" in skills["foundation-test"]
