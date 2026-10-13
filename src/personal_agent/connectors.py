"""Connectors to outside creative services: Canva and Higgsfield.

Each connector's tools are registered with the tool gateway, so the gateway's
rules hold for them: a node may call only the tools on its allowlist, a payload
names its client, and a restricted client's work stays out of a connector the
owner has not approved for confidential work (config/providers.yaml).

Every tool here is draft-class: it makes something and saves it to the
client's own folder, or makes a design in the owner's own Canva account. None
sends anything to a third person. Canva's API has no call that shares a design
with someone, so sharing stays a thing the owner does in Canva.

What leaves this server: a prompt goes to Higgsfield; a title, and an image the
owner picks, go to Canva.

Written from the two services' published API references (October 2026):

  Canva Connect   OAuth 2.0 with PKCE; the owner signs in once and the tokens
                  are kept in the database. A refresh token works once, so each
                  refresh stores the new one before anything else happens.
  Higgsfield      "Authorization: Key ID:SECRET"; a request is submitted to a
                  model endpoint, polled at the status_url it returns, and its
                  output downloaded. The model endpoints are in
                  config/connectors.yaml, since they depend on the account.

Neither has been run against a live account from this code.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import urlencode, urlsplit

import httpx

from personal_agent.clients import ClientScope
from personal_agent.config import Settings
from personal_agent.db import Database
from personal_agent.gateway.tools import ToolClass, ToolGateway, ToolSpec

CANVA_API = "https://api.canva.com/rest/v1"
CANVA_AUTHORIZE = "https://www.canva.com/api/oauth/authorize"
CANVA_SCOPES = "design:content:read design:content:write design:meta:read asset:read asset:write"
HIGGSFIELD_API = "https://api.higgsfield.ai"
HIGGSFIELD_ENDPOINTS = {"image": "higgsfield-ai/soul/v2/standard", "video": "wan/v2.7/text-to-video"}
# Sizes offered for a Canva design: a preset Canva names, or a width and height in pixels.
CANVA_SIZES = {
    "presentation": ("Presentation", None), "doc": ("Document", None), "whiteboard": ("Whiteboard", None),
    "square": ("Square post, 1080 by 1080", (1080, 1080)), "story": ("Story, 1080 by 1920", (1080, 1920)),
    "landscape": ("Landscape, 1920 by 1080", (1920, 1080)), "a4": ("A4 portrait, 2480 by 3508", (2480, 3508)),
}
EXPORT_FORMATS = {"pdf": {"type": "pdf"}, "png": {"type": "png"}, "jpg": {"type": "jpg", "quality": 90}}
MAX_DOWNLOAD_BYTES = 200_000_000
# Tests replace these: a stand-in for the network, and no waiting between polls.
TRANSPORT: httpx.BaseTransport | None = None
PAUSE = 4.0
_refresh_lock = threading.Lock()


class ConnectorNotReady(Exception):
    """The tool cannot run, or the service refused or failed. The message says which, and what to do."""


@dataclass(frozen=True)
class ConnectorTool:
    name: str
    cls: ToolClass
    does: str


@dataclass(frozen=True)
class Connector:
    name: str  # also the name to list under approved_for_confidential
    label: str
    needs: tuple[str, ...]  # settings the server must hold
    tools: tuple[ConnectorTool, ...]

    def missing(self) -> list[str]:
        return [name for name in self.needs if not os.environ.get(name)]


CONNECTORS = (
    Connector(
        name="canva", label="Canva", needs=("CANVA_CLIENT_ID", "CANVA_CLIENT_SECRET"),
        tools=(
            ConnectorTool("canva_create_design", ToolClass.DRAFT, "Makes a design in your Canva account, blank or holding one image from the folder."),
            ConnectorTool("canva_export_design", ToolClass.DRAFT, "Saves a Canva design to the client's folder as a PDF or images."),
        ),
    ),
    Connector(
        name="higgsfield", label="Higgsfield", needs=("HIGGSFIELD_API_KEY_ID", "HIGGSFIELD_API_KEY_SECRET"),
        tools=(
            ConnectorTool("higgsfield_generate_image", ToolClass.DRAFT, "Generates an image from a prompt and saves it to the client's folder."),
            ConnectorTool("higgsfield_generate_video", ToolClass.DRAFT, "Generates a short video from a prompt and saves it to the client's folder."),
        ),
    ),
)
BY_NAME = {c.name: c for c in CONNECTORS}


def _http(timeout: float = 60.0) -> httpx.Client:
    return httpx.Client(transport=TRANSPORT, timeout=timeout)


def _why(response: httpx.Response) -> str:
    try:
        body = response.json()
        detail = body.get("message") or body.get("error_description") or body.get("detail") or body.get("error") or ""
        if isinstance(detail, dict):
            detail = detail.get("message") or json.dumps(detail)
    except ValueError:
        detail = response.text
    return f"{response.status_code} {str(detail)[:300]}".strip()


def _download(url: str) -> bytes:
    """An output file from the service. Only https, and only up to a size a folder can hold."""
    if urlsplit(url).scheme != "https" and TRANSPORT is None:
        raise ConnectorNotReady("the service returned a link that is not https, so the file was not fetched")
    with _http(300.0) as http:
        response = http.get(url, follow_redirects=True)
    if response.status_code != 200:
        raise ConnectorNotReady(f"the finished file could not be downloaded ({response.status_code})")
    if len(response.content) > MAX_DOWNLOAD_BYTES:
        raise ConnectorNotReady("the finished file is over 200 MB, so it was not saved")
    return response.content


def _slug(text: str, fallback: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or fallback


def _suffix(url: str, default: str) -> str:
    ext = PurePosixPath(urlsplit(url).path).suffix.lower()
    return ext if re.fullmatch(r"\.[a-z0-9]{2,5}", ext) else default


# --- Canva ---------------------------------------------------------------------------------
class Canva:
    """The owner's Canva account, reached with the owner's own sign-in."""

    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _basic() -> str:
        pair = f"{os.environ.get('CANVA_CLIENT_ID', '')}:{os.environ.get('CANVA_CLIENT_SECRET', '')}"
        return "Basic " + base64.b64encode(pair.encode()).decode()

    def connected(self) -> bool:
        return bool((self.db.get_connector("canva") or {}).get("refresh_token"))

    # The owner's sign-in: this server sends the owner to Canva and Canva sends them back with a code.
    def authorize_url(self, redirect_uri: str) -> str:
        verifier, state = secrets.token_urlsafe(64), secrets.token_urlsafe(24)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        self.db.set_connector("canva:pending", {"state": state, "verifier": verifier, "redirect_uri": redirect_uri,
                                                "at": time.time()})
        return CANVA_AUTHORIZE + "?" + urlencode({
            "code_challenge": challenge, "code_challenge_method": "S256", "scope": CANVA_SCOPES,
            "response_type": "code", "client_id": os.environ.get("CANVA_CLIENT_ID", ""), "state": state,
            "redirect_uri": redirect_uri,
        })

    def finish(self, code: str, state: str) -> None:
        pending = self.db.get_connector("canva:pending") or {}
        self.db.delete_connector("canva:pending")  # one sign-in, one use
        if not pending or not secrets.compare_digest(str(pending.get("state")), state) or time.time() - pending["at"] > 900:
            raise ConnectorNotReady("that Canva sign-in did not start here, or took over 15 minutes. Start it again")
        self._store(self._token_call({
            "grant_type": "authorization_code", "code": code, "code_verifier": pending["verifier"],
            "redirect_uri": pending["redirect_uri"],
        }))

    def disconnect(self) -> None:
        self.db.delete_connector("canva")

    def _token_call(self, form: dict) -> dict:
        with _http() as http:
            response = http.post(f"{CANVA_API}/oauth/token", data=form, headers={"Authorization": self._basic()})
        if response.status_code != 200:
            raise ConnectorNotReady(f"Canva refused the sign-in ({_why(response)})")
        return response.json()

    def _store(self, token: dict) -> None:
        self.db.set_connector("canva", {
            "access_token": token["access_token"], "refresh_token": token["refresh_token"],
            "expires_at": time.time() + int(token.get("expires_in", 3600)) - 120, "scope": token.get("scope", ""),
        })

    def _bearer(self) -> str:
        # One refresh at a time: Canva's refresh token works once, and the next one must be saved before it is used.
        with _refresh_lock:
            held = self.db.get_connector("canva") or {}
            if not held.get("refresh_token"):
                raise ConnectorNotReady("Canva is not connected. Connect it on the Studio screen")
            if time.time() >= held.get("expires_at", 0):
                try:
                    self._store(self._token_call({"grant_type": "refresh_token", "refresh_token": held["refresh_token"]}))
                except ConnectorNotReady:
                    self.db.delete_connector("canva")  # the sign-in has lapsed; the owner connects again
                    raise ConnectorNotReady("Canva's sign-in has lapsed. Connect it again on the Studio screen")
                held = self.db.get_connector("canva")
            return "Bearer " + held["access_token"]

    def _call(self, method: str, path: str, **kwargs) -> dict:
        headers = {"Authorization": self._bearer(), **kwargs.pop("headers", {})}
        try:
            with _http() as http:
                response = http.request(method, f"{CANVA_API}{path}", headers=headers, **kwargs)
        except httpx.HTTPError as e:
            raise ConnectorNotReady(f"Canva could not be reached ({type(e).__name__})") from e
        if response.status_code != 200:
            raise ConnectorNotReady(f"Canva refused the request ({_why(response)})")
        return response.json()

    def _wait(self, path: str, what: str, minutes: int = 3) -> dict:
        """Poll a Canva job until it succeeds; a failure or a long wait is reported, never hidden."""
        deadline = time.time() + minutes * 60
        while True:
            job = self._call("GET", path)["job"]
            if job["status"] == "success":
                return job
            if job["status"] == "failed":
                raise ConnectorNotReady(f"Canva could not finish the {what} ({(job.get('error') or {}).get('message', 'no reason given')})")
            if time.time() > deadline:
                raise ConnectorNotReady(f"Canva had not finished the {what} after {minutes} minutes")
            time.sleep(PAUSE)

    def upload_image(self, name: str, data: bytes) -> str:
        started = self._call("POST", "/asset-uploads", content=data, headers={
            "Content-Type": "application/octet-stream",
            "Asset-Upload-Metadata": json.dumps({"name_base64": base64.b64encode(name[:50].encode()).decode()}),
        })["job"]
        job = started if started["status"] == "success" else self._wait(f"/asset-uploads/{started['id']}", "upload")
        return job["asset"]["id"]

    def create_design(self, title: str, size: str, image: tuple[str, bytes] | None = None) -> dict:
        label, pixels = CANVA_SIZES[size]
        body: dict = {
            "design_type": {"type": "custom", "width": pixels[0], "height": pixels[1]} if pixels else {"type": "preset", "name": size},
            "title": title[:255],
        }
        if image:
            body["asset_id"] = self.upload_image(*image)
        design = self._call("POST", "/designs", json=body)["design"]
        return {"design_id": design["id"], "title": design.get("title") or title, "size": label,
                "edit_url": (design.get("urls") or {}).get("edit_url"), "view_url": (design.get("urls") or {}).get("view_url")}

    def export_design(self, design_id: str, kind: str) -> list[str]:
        started = self._call("POST", "/exports", json={"design_id": design_id, "format": EXPORT_FORMATS[kind]})["job"]
        job = started if started["status"] == "success" else self._wait(f"/exports/{started['id']}", "export")
        return job.get("urls") or []


# --- Higgsfield ------------------------------------------------------------------------------
def higgsfield_generate(endpoint: str, body: dict, idempotency_key: str, minutes: int) -> dict:
    """Submit one generation, wait for it, and return the finished request as Higgsfield describes it."""
    headers = {
        "Authorization": f"Key {os.environ.get('HIGGSFIELD_API_KEY_ID', '')}:{os.environ.get('HIGGSFIELD_API_KEY_SECRET', '')}",
        "Idempotency-Key": idempotency_key,  # the same request, sent twice after a dropped connection, is made once
    }
    try:
        with _http() as http:
            sent = http.post(f"{HIGGSFIELD_API}/{endpoint.strip('/')}", json=body, headers=headers)
            if sent.status_code not in (200, 201, 202):
                raise ConnectorNotReady(f"Higgsfield refused the request ({_why(sent)})")
            request = sent.json()
            deadline = time.time() + minutes * 60
            while request.get("status") in ("queued", "in_progress"):
                if time.time() > deadline:
                    raise ConnectorNotReady(
                        f"Higgsfield had not finished after {minutes} minutes (request {request.get('request_id')}). "
                        "It may still finish there; nothing was saved here"
                    )
                time.sleep(PAUSE)
                polled = http.get(request["status_url"], headers={"Authorization": headers["Authorization"]})
                if polled.status_code != 200:
                    raise ConnectorNotReady(f"Higgsfield would not report on the request ({_why(polled)})")
                request = {**request, **polled.json()}
    except httpx.HTTPError as e:
        raise ConnectorNotReady(f"Higgsfield could not be reached ({type(e).__name__})") from e
    if request.get("status") == "nsfw":
        raise ConnectorNotReady("Higgsfield's content check turned this prompt or its result down")
    if request.get("status") != "completed":
        raise ConnectorNotReady(f"Higgsfield did not make it: {request.get('status')} {str(request.get('error') or '')[:200]}".strip())
    return request


# --- the tools ---------------------------------------------------------------------------------
def register(tools: ToolGateway, db: Database, settings: Settings, clients) -> None:
    canva = Canva(db)
    endpoints = {**HIGGSFIELD_ENDPOINTS, **((settings.connectors.get("higgsfield") or {}).get("endpoints") or {})}

    def guarded(connector: Connector, tool: ConnectorTool, fn):
        def call(payload: dict) -> dict:
            client = payload.get("client")
            if not client:
                raise ConnectorNotReady(f"{tool.name} was not run: the call names no client.")
            if db.is_restricted(client) and connector.name not in settings.confidential_providers:
                raise ConnectorNotReady(
                    f"{tool.name} was not run: {client} is restricted and {connector.label} is not approved for confidential work."
                )
            if connector.missing():
                raise ConnectorNotReady(
                    f"{tool.name} was not run: {connector.label} is not connected. The server has no {' or '.join(connector.missing())}."
                )
            return fn(payload, ClientScope(client))

        return call

    def save(scope: ClientScope, stem: str, urls: list[str], default_ext: str) -> list[str]:
        return [
            clients.store_file(scope, f"{stem}{'-' + str(n) if len(urls) > 1 else ''}{_suffix(url, default_ext)}", _download(url))
            for n, url in enumerate(urls, 1)
        ]

    def canva_create(payload: dict, scope: ClientScope) -> dict:
        size = payload.get("size") if payload.get("size") in CANVA_SIZES else "presentation"
        title = str(payload.get("title") or "").strip()
        if not title:
            raise ConnectorNotReady("canva_create_design was not run: the design needs a title.")
        image = None
        if payload.get("image_path"):
            image = (PurePosixPath(payload["image_path"]).name, clients.read_bytes(scope, payload["image_path"]))
        return canva.create_design(title, size, image)

    def canva_export(payload: dict, scope: ClientScope) -> dict:
        kind = payload.get("format") if payload.get("format") in EXPORT_FORMATS else "pdf"
        design_id = str(payload.get("design_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,40}", design_id):
            raise ConnectorNotReady("canva_export_design was not run: that is not a Canva design ID.")
        urls = canva.export_design(design_id, kind)
        if not urls:
            raise ConnectorNotReady("Canva finished the export and returned no file.")
        stem = f"canva-{_slug(str(payload.get('title') or ''), design_id.lower())}"
        return {"design_id": design_id, "files": save(scope, stem, urls, "." + kind)}

    def generate(kind: str, minutes: int):
        def run(payload: dict, scope: ClientScope) -> dict:
            prompt = str(payload.get("prompt") or "").strip()
            if not prompt:
                raise ConnectorNotReady(f"higgsfield_generate_{kind} was not run: it needs a prompt.")
            body: dict = {"prompt": prompt[:4000]}
            if kind == "video":
                body["duration"] = max(2, min(15, int(payload.get("duration") or 5)))
                if payload.get("aspect_ratio") in ("16:9", "9:16", "1:1", "4:3", "3:4"):
                    body["aspect_ratio"] = payload["aspect_ratio"]
            key = hashlib.sha256(json.dumps([scope.name, kind, body, payload.get("nonce")], sort_keys=True).encode()).hexdigest()
            done = higgsfield_generate(endpoints[kind], body, key, minutes)
            urls = [i["url"] for i in done.get("images") or [] if i.get("url")] if kind == "image" else \
                [u for u in [(done.get("video") or {}).get("url")] if u]
            if not urls:
                raise ConnectorNotReady("Higgsfield reported the request complete and returned no file.")
            stem = f"higgsfield-{_slug(prompt, kind)}-{str(done.get('request_id') or key)[:8]}"
            return {"request_id": done.get("request_id"), "files": save(scope, stem, urls, ".jpg" if kind == "image" else ".mp4")}

        return run

    handlers = {
        "canva_create_design": canva_create, "canva_export_design": canva_export,
        "higgsfield_generate_image": generate("image", 5), "higgsfield_generate_video": generate("video", 15),
    }
    for connector in CONNECTORS:
        for tool in connector.tools:
            tools.register(ToolSpec(name=tool.name, cls=tool.cls, fn=guarded(connector, tool, handlers[tool.name])))


def status(db: Database) -> list[dict]:
    """Each connector's state, for the Settings and Studio screens."""
    out = []
    for c in CONNECTORS:
        if c.missing():
            ready, state = False, f"Not connected: the server has no {' or '.join(c.missing())}"
        elif c.name == "canva" and not Canva(db).connected():
            ready, state = False, "The keys are saved. Sign in to Canva on the Studio screen to finish"
        else:
            ready, state = True, "Connected"
        out.append({"name": c.name, "label": c.label, "ready": ready, "state": state,
                    "tools": [(t.name, t.cls.value, t.does) for t in c.tools]})
    return out
