"""The working rules outlast a deploy: the owner's accepted changes stay, shipped updates still arrive."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from personal_agent.api.main import create_app
from personal_agent.harness.models import RecordedGateway
from personal_agent.skills.sync import baseline_dir, diverged, sync_library

V1, V2, MINE = "Tone: concise.\n", "Tone: concise and warm.\n", "Tone: concise.\nNever open with a greeting.\n"


def _ship(shipped: Path, text: str, skill: str = "brief-evaluation") -> None:
    (shipped / skill).mkdir(parents=True, exist_ok=True)
    (shipped / skill / "SKILL.md").write_text(text, encoding="utf-8")


def _live(live: Path, skill: str = "brief-evaluation") -> Path:
    return live / skill / "SKILL.md"


def test_a_first_start_copies_what_shipped(tmp_path):
    shipped, live = tmp_path / "image", tmp_path / "data" / "skills"
    _ship(shipped, V1)
    assert sync_library(shipped, live) == {"added": ["brief-evaluation"], "updated": [], "diverged": []}
    assert _live(live).read_text() == V1 and (baseline_dir(live) / "brief-evaluation" / "SKILL.md").read_text() == V1
    assert sync_library(shipped, live) == {"added": [], "updated": [], "diverged": []}  # a second start changes nothing


def test_an_untouched_skill_takes_the_shipped_update(tmp_path):
    shipped, live = tmp_path / "image", tmp_path / "data" / "skills"
    _ship(shipped, V1)
    sync_library(shipped, live)
    _ship(shipped, V2)  # a deploy with new rules
    assert sync_library(shipped, live)["updated"] == ["brief-evaluation"]
    assert _live(live).read_text() == V2


def test_the_owners_change_survives_a_deploy_that_ships_the_same_file(tmp_path):
    shipped, live = tmp_path / "image", tmp_path / "data" / "skills"
    _ship(shipped, V1)
    sync_library(shipped, live)
    _live(live).write_text(MINE, encoding="utf-8")  # an accepted proposal
    assert sync_library(shipped, live) == {"added": [], "updated": [], "diverged": []}  # the next deploy
    assert _live(live).read_text() == MINE and diverged(shipped, live) == []


def test_when_both_changed_the_owners_text_stays_and_the_owner_chooses(settings, tmp_path):
    shipped = tmp_path / "image"
    _ship(shipped, V1)
    _ship(shipped, "name: foundation-test\n", "foundation-test")
    settings.shipped_skills_dir, settings.skills_dir = shipped, tmp_path / "data" / "skills"
    create_app(settings, gateway=RecordedGateway({}))  # first start
    live = _live(settings.skills_dir)
    live.write_text(MINE, encoding="utf-8")
    _ship(shipped, V2)

    client = TestClient(create_app(settings, gateway=RecordedGateway({})), follow_redirects=False)  # the deploy
    assert live.read_text() == MINE
    page = client.get("/library").text
    assert "Updates that met your changes" in page and "+Tone: concise and warm." in page
    assert client.post("/skills/foundation-test/shipped", data={"choice": "shipped"}).status_code == 404

    kept = client.post("/skills/brief-evaluation/shipped", data={"choice": "mine"})
    assert kept.json()["took_shipped"] is False and live.read_text() == MINE
    assert "Updates that met your changes" not in client.get("/library").text  # asked once, not at every start
    assert sync_library(shipped, settings.skills_dir)["diverged"] == []

    _ship(shipped, V1 + "A later rule.\n")
    sync_library(shipped, settings.skills_dir)
    taken = client.post("/skills/brief-evaluation/shipped", data={"choice": "shipped"}, headers={"accept": "text/html"})
    assert taken.status_code == 303 and taken.headers["location"] == "/library"
    assert live.read_text() == V1 + "A later rule.\n"
    assert client.get("/skills").json()["skills"]["brief-evaluation"] == V1 + "A later rule.\n"  # the agent reads it at once
