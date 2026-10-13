"""Canva and Higgsfield: the real calls, against a stand-in for the two services."""

from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from personal_agent import connectors
from personal_agent.api.main import create_app
from personal_agent.config import Workstream
from personal_agent.gateway.tools import ToolRefused
from personal_agent.harness.models import RecordedGateway

KEYS = {"CANVA_CLIENT_ID": "canva-id", "CANVA_CLIENT_SECRET": "canva-secret-not-real",
        "HIGGSFIELD_API_KEY_ID": "hf-id", "HIGGSFIELD_API_KEY_SECRET": "hf-secret-not-real"}


class Services:
    """Answers as Canva and Higgsfield do in their API references, and remembers what it was sent."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.refresh_tokens: set[str] = set()  # each works once
        self.issued = 1
        self.polls = 0
        self.verdict = "completed"

    def _tokens(self) -> httpx.Response:
        self.issued += 1
        self.refresh_tokens.add(f"refresh-{self.issued}")
        return httpx.Response(200, json={"access_token": f"access-{self.issued}", "refresh_token": f"refresh-{self.issued}",
                                         "expires_in": 14400, "token_type": "Bearer", "scope": connectors.CANVA_SCOPES})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        host, path = request.url.host, request.url.path
        if host == "api.canva.com":
            if path.endswith("/oauth/token"):
                form = parse_qs(request.content.decode())
                if form["grant_type"] == ["refresh_token"]:
                    token = form["refresh_token"][0]
                    if token not in self.refresh_tokens:
                        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "refresh token already used"})
                    self.refresh_tokens.discard(token)
                    return self._tokens()
                self.code_verifier = form["code_verifier"][0]
                return self._tokens()
            if path.endswith("/asset-uploads"):
                return httpx.Response(200, json={"job": {"id": "up-1", "status": "success", "asset": {"id": "asset-9"}}})
            if path.endswith("/designs"):
                return httpx.Response(200, json={"design": {"id": "DAGdesign01", "title": json.loads(request.content)["title"],
                                                            "urls": {"edit_url": "https://www.canva.com/design/DAGdesign01/edit",
                                                                     "view_url": "https://www.canva.com/design/DAGdesign01/view"}}})
            if path.endswith("/exports"):
                return httpx.Response(200, json={"job": {"id": "ex-1", "status": "in_progress"}})
            if path.endswith("/exports/ex-1"):
                return httpx.Response(200, json={"job": {"id": "ex-1", "status": "success",
                                                         "urls": ["https://export.example/p1.png", "https://export.example/p2.png"]}})
        if host == "api.higgsfield.ai":
            if request.method == "POST":
                return httpx.Response(200, json={"status": "queued", "request_id": "req-12345678",
                                                 "status_url": "https://api.higgsfield.ai/requests/req-12345678/status",
                                                 "cancel_url": "https://api.higgsfield.ai/requests/req-12345678/cancel"})
            self.polls += 1
            if self.polls < 2:
                return httpx.Response(200, json={"status": "in_progress", "request_id": "req-12345678"})
            done = {"status": self.verdict, "request_id": "req-12345678"}
            if self.verdict == "completed":
                done.update({"images": [{"url": "https://cdn.example/out.jpg"}], "video": {"url": "https://cdn.example/out.mp4"}})
            return httpx.Response(200, json=done)
        if host in ("cdn.example", "export.example"):
            return httpx.Response(200, content=b"bytes of " + path.encode())
        return httpx.Response(404, json={"message": "no such thing"})

    def sent_to(self, host: str, method: str = "POST") -> list[httpx.Request]:
        return [r for r in self.calls if r.url.host == host and r.method == method]


@pytest.fixture
def services(monkeypatch) -> Services:
    fake = Services()
    monkeypatch.setattr(connectors, "TRANSPORT", httpx.MockTransport(fake))
    monkeypatch.setattr(connectors, "PAUSE", 0)
    for name, value in KEYS.items():
        monkeypatch.setenv(name, value)
    return fake


def _app(settings):
    settings.workstreams = [Workstream(name="AltBank")]
    settings.public_url = "https://agent.example"
    app = create_app(settings, gateway=RecordedGateway({}))
    return app, TestClient(app, follow_redirects=False)


def _make(client, **data) -> dict:
    sent = client.post("/studio", data={"workstream": "AltBank", **data})
    assert sent.status_code == 303 and sent.headers["location"].startswith("/studio?wait="), sent.headers.get("location")
    run_id = sent.headers["location"].split("wait=")[1].split("#")[0]
    return client.get(f"/runs/{run_id}").json()


def _connect_canva(client, services) -> None:
    there = client.get("/connectors/canva/connect").headers["location"]
    state = parse_qs(urlsplit(there).query)["state"][0]
    assert client.get(f"/connectors/canva/callback?code=the-code&state={state}").status_code == 303


# --- the gateway's rules still hold -------------------------------------------------------
def test_the_tools_are_draft_class_and_nothing_shares_a_design(settings, services):
    app, _ = _app(settings)
    registered = dict(app.state.engine.harness.tools.registered())
    for tool in ("canva_create_design", "canva_export_design", "higgsfield_generate_image", "higgsfield_generate_video"):
        assert registered[tool] == "draft"
    assert "canva_share_design" not in registered


def test_a_call_is_refused_off_the_allowlist_for_another_client_or_a_restricted_one(settings, services):
    app, client = _app(settings)
    engine = app.state.engine
    run_id = engine.start_run("foundation-test", "AltBank", {"objective": "x"}, work=False)
    tools = engine.harness.tools
    with pytest.raises(ToolRefused):
        tools.call(run_id, "make", [], "higgsfield_generate_image", {"client": "AltBank", "prompt": "a poster"})
    with pytest.raises(ToolRefused):
        tools.call(run_id, "make", ["higgsfield_generate_image"], "higgsfield_generate_image", {"client": "Other", "prompt": "x"})
    engine.db.set_restricted("AltBank", True)
    with pytest.raises(connectors.ConnectorNotReady, match="AltBank is restricted and Higgsfield is not approved"):
        tools.call(run_id, "make", ["higgsfield_generate_image"], "higgsfield_generate_image", {"client": "AltBank", "prompt": "x"})
    assert services.sent_to("api.higgsfield.ai") == []  # nothing left the server


def test_with_no_keys_the_studio_says_what_is_missing_and_a_run_fails_with_the_reason(settings, services, monkeypatch):
    for name in KEYS:
        monkeypatch.delenv(name)
    _, client = _app(settings)
    page = client.get("/studio").text
    assert "the server has no HIGGSFIELD_API_KEY_ID or HIGGSFIELD_API_KEY_SECRET" in page
    assert "the server has no CANVA_CLIENT_ID or CANVA_CLIENT_SECRET" in page
    run = _make(client, tool="higgsfield_generate_image", prompt="a poster")
    assert run["status"] == "failed" and "Higgsfield is not connected" in run["error_reason"]
    assert "--key canva" in client.get(client.get("/connectors/canva/connect").headers["location"]).text


# --- Higgsfield ---------------------------------------------------------------------------
def test_an_image_is_requested_polled_downloaded_and_saved_to_the_workstream(settings, services):
    _, client = _app(settings)
    run = _make(client, tool="higgsfield_generate_image", prompt="A quiet alpine lake at sunrise")
    assert run["status"] == "done" and run["workflow"] == "studio"
    submit = services.sent_to("api.higgsfield.ai")[0]
    assert submit.url.path == "/higgsfield-ai/soul/v2/standard" and json.loads(submit.content) == {"prompt": "A quiet alpine lake at sunrise"}
    assert submit.headers["authorization"] == "Key hf-id:hf-secret-not-real" and len(submit.headers["idempotency-key"]) == 64
    assert services.polls == 2  # it waited through "in progress"
    files = client.get("/clients/AltBank/files").json()
    assert [f["path"] for f in files] == ["higgsfield-a-quiet-alpine-lake-at-sunrise-req-1234.jpg"]
    page = client.get("/studio").text
    assert "Saved to the AltBank folder" in page and "hf-secret-not-real" not in page
    assert "Studio" not in client.get("/work").text.split("<main")[1].split("board")[1]  # a Studio run is not a task on the board


def test_a_video_goes_to_the_video_endpoint_with_its_length_and_shape(settings, services):
    settings.connectors = {"higgsfield": {"endpoints": {"video": "wan/v3/text-to-video"}}}  # the account's own model
    _, client = _app(settings)
    run = _make(client, tool="higgsfield_generate_video", prompt="Waves at dusk", duration="99", aspect_ratio="9:16")
    submit = services.sent_to("api.higgsfield.ai")[0]
    assert submit.url.path == "/wan/v3/text-to-video"
    assert json.loads(submit.content) == {"prompt": "Waves at dusk", "duration": 15, "aspect_ratio": "9:16"}
    assert run["outputs"]["result"]["files"] == ["higgsfield-waves-at-dusk-req-1234.mp4"]


def test_a_request_the_content_check_turns_down_saves_nothing_and_says_so(settings, services):
    services.verdict = "nsfw"
    _, client = _app(settings)
    run = _make(client, tool="higgsfield_generate_image", prompt="x")
    assert run["status"] == "failed" and "content check turned this" in run["error_reason"]
    assert client.get("/clients/AltBank/files").json() == []
    assert "content check turned this" in client.get("/studio").text


# --- Canva --------------------------------------------------------------------------------
def test_the_owner_signs_in_to_canva_once_and_a_sign_in_that_did_not_start_here_is_refused(settings, services):
    app, client = _app(settings)
    assert "Sign in to Canva" in client.get("/studio").text
    there = client.get("/connectors/canva/connect").headers["location"]
    query = parse_qs(urlsplit(there).query)
    assert there.startswith("https://www.canva.com/api/oauth/authorize?") and query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["https://agent.example/connectors/canva/callback"] and query["client_id"] == ["canva-id"]

    forged = client.get("/connectors/canva/callback?code=the-code&state=someone-elses")
    assert "did not start here" in client.get(forged.headers["location"]).text
    assert not connectors.Canva(app.state.engine.db).connected()

    _connect_canva(client, services)
    token_call = services.sent_to("api.canva.com")[-1]
    assert token_call.headers["authorization"] == "Basic " + base64.b64encode(b"canva-id:canva-secret-not-real").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(services.code_verifier.encode()).digest()).decode().rstrip("=")
    again = parse_qs(urlsplit(client.get("/connectors/canva/connect").headers["location"]).query)
    assert again["code_challenge"] != [challenge]  # a fresh verifier for each sign-in
    page = client.get("/studio").text
    assert "Canva is connected to your account." in page and "access-2" not in page and "refresh-2" not in page


def test_a_design_is_made_with_an_image_from_the_folder_and_brought_back_as_files(settings, services):
    app, client = _app(settings)
    _connect_canva(client, services)
    client.post("/clients/AltBank/files", files={"file": ("hero.png", b"\x89PNG fake")})
    made = _make(client, tool="canva_create_design", title="Launch poster", size="square", image_path="hero.png")
    assert made["status"] == "done", made["error_reason"]
    upload = next(r for r in services.calls if r.url.path.endswith("/asset-uploads"))
    assert upload.content == b"\x89PNG fake" and upload.headers["authorization"] == "Bearer access-2"
    assert json.loads(upload.headers["asset-upload-metadata"]) == {"name_base64": base64.b64encode(b"hero.png").decode()}
    design = next(r for r in services.calls if r.url.path.endswith("/designs"))
    assert json.loads(design.content) == {"design_type": {"type": "custom", "width": 1080, "height": 1080},
                                          "title": "Launch poster", "asset_id": "asset-9"}
    page = client.get("/studio").text
    assert "DAGdesign01" in page and "Open it in Canva" in page and "Launch poster · AltBank" in page

    back = _make(client, tool="canva_export_design", design_pick="DAGdesign01", format="png")
    assert back["outputs"]["result"]["files"] == ["canva-dagdesign01-1.png", "canva-dagdesign01-2.png"]
    export = next(r for r in services.calls if r.url.path.endswith("/exports"))
    assert json.loads(export.content) == {"design_id": "DAGdesign01", "format": {"type": "png"}}
    bad = _make(client, tool="canva_export_design", design_id="../../etc", format="pdf")
    assert bad["status"] == "failed" and "not a Canva design ID" in bad["error_reason"]


def test_an_expired_token_is_refreshed_once_and_the_new_one_kept_and_a_lapsed_sign_in_asks_the_owner_back(settings, services):
    app, client = _app(settings)
    _connect_canva(client, services)
    db = app.state.engine.db
    held = db.get_connector("canva")
    db.set_connector("canva", {**held, "expires_at": 0})  # the four hours are up
    _make(client, tool="canva_create_design", title="One", size="doc")
    assert db.get_connector("canva")["refresh_token"] == "refresh-3"  # the single-use token was replaced
    design = [r for r in services.calls if r.url.path.endswith("/designs")][-1]
    assert design.headers["authorization"] == "Bearer access-3"

    db.set_connector("canva", {**db.get_connector("canva"), "expires_at": 0, "refresh_token": "refresh-2"})  # one already used
    run = _make(client, tool="canva_create_design", title="Two", size="doc")
    assert run["status"] == "failed" and "sign-in has lapsed" in run["error_reason"]
    assert not connectors.Canva(db).connected() and "Sign in to Canva" in client.get("/studio").text


def test_settings_lists_each_connector_and_shows_no_secret(settings, services):
    _, client = _app(settings)
    page = client.get("/cost").text
    assert "Sign in to Canva on the Studio screen to finish" in page and "<strong>Higgsfield</strong>" in page
    assert "<code>canva_export_design</code> (draft)" in page
    for secret in ("canva-secret-not-real", "hf-secret-not-real"):
        assert secret not in page
