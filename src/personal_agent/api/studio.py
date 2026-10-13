"""The Studio screen: make an image or a video with Higgsfield, make a design in
Canva or bring one back as a file, and connect the owner's Canva account."""

from __future__ import annotations

import re
from html import escape
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from personal_agent import connectors
from personal_agent.api import ui
from personal_agent.clients import ClientScope
from personal_agent.costs import MonthlyCapReached
from personal_agent.engine import studio

IMAGE_FILE = re.compile(r".+\.(png|jpe?g|webp|gif)$", re.IGNORECASE)


def install(app: FastAPI, *, settings, db, engine, render, ws_api, status_words) -> None:
    icon = ui.icon
    canva = connectors.Canva(db)

    def redirect_uri(request: Request) -> str:
        return (settings.public_url or str(request.base_url)).rstrip("/") + "/connectors/canva/callback"

    def notice(text: str) -> RedirectResponse:
        return RedirectResponse(f"/studio?notice={quote(text)}", status_code=303)

    # --- Canva's sign-in ----------------------------------------------------------------------
    @app.get("/connectors/canva/connect")
    def canva_connect(request: Request):
        if connectors.BY_NAME["canva"].missing():
            return notice("The server has no Canva client ID and secret yet. Add them with ./deploy/setup.sh --key canva")
        return RedirectResponse(canva.authorize_url(redirect_uri(request)), status_code=303)

    @app.get("/connectors/canva/callback")
    def canva_callback(code: str = "", state: str = "", error: str = ""):
        if error or not code:
            return notice(f"Canva did not complete the sign-in{': ' + error if error else ''}.")
        try:
            canva.finish(code, state)
        except connectors.ConnectorNotReady as e:
            return notice(f"{e}.")
        db.audit(None, "connector_connected", {"connector": "canva"})
        return notice("Canva is connected.")

    @app.post("/connectors/canva/disconnect")
    def canva_disconnect():
        canva.disconnect()
        db.audit(None, "connector_disconnected", {"connector": "canva"})
        return notice("Canva is disconnected. This server holds no Canva sign-in now.")

    # --- the screen ---------------------------------------------------------------------------
    @app.get("/studio", response_class=HTMLResponse)
    def studio_page(request: Request, ws: str = "", notice: str = "", wait: str = "") -> str:
        ws = ws_api.known(ws)
        state = {c["name"]: c for c in connectors.status(db)}
        names = engine.harness.clients.list_clients()
        streams = '<option value="">Choose a workstream</option>' + "".join(
            f'<option value="{escape(n)}"{" selected" if n == ws else ""}>{escape(n)}</option>' for n in names
        )
        runs = [r for r in db.list_runs() if r["workflow"] == studio.WORKFLOW and (not ws or r["client"] == ws)][:30]
        designs = []  # designs made here, offered for export
        for r in runs:
            made = ((r["outputs"] or {}).get("result") or {})
            if (r["outputs"] or {}).get("tool") == "canva_create_design" and made.get("design_id"):
                designs.append((made["design_id"], made.get("title") or made["design_id"], r["client"]))
        images = ""
        if ws:
            images = "".join(
                f'<option value="{escape(f["path"])}">{escape(f["path"])}</option>'
                for f in engine.harness.clients.list_files(ClientScope(ws)) if IMAGE_FILE.fullmatch(f["path"])
            )

        def card(r: dict) -> str:
            inputs, out = r["inputs"] or {}, ((r["outputs"] or {}).get("result") or {})
            params = inputs.get("params") or {}
            about = params.get("prompt") or params.get("title") or params.get("design_id") or ""
            if out.get("files"):
                body = f'<p>Saved to the {escape(r["client"])} folder: {", ".join(f"<code>{escape(f)}</code>" for f in out["files"])}.</p>'
            elif out.get("design_id"):
                link = f' <a href="{escape(out["edit_url"])}" rel="noopener" target="_blank">Open it in Canva</a>.' if out.get("edit_url") else ""
                body = f'<p>Made in your Canva account as <code>{escape(out["design_id"])}</code> ({escape(out.get("size") or "")}).{link}</p>'
            elif r["status"] == "failed":
                body = f'<p class="error">{escape(r["error_reason"] or "It failed.")}</p>'
            else:
                body = '<p class="quiet">The service is working on it. This page updates itself.</p>'
            return f"""<section class="card" id="s-{r["id"]}">
  <p class="chips"><span class="tag c-purple">{escape(studio.TOOLS.get(inputs.get("tool"), "Studio"))}</span>
    <span class="tag c-slate">{escape(r["client"])}</span>
    <span class="s-{escape(r["status"])}">{escape(status_words.get(r["status"], r["status"]))}</span></p>
  <h3>{escape(str(about)[:200])}</h3>
  {body}
</section>"""

        def gate(name: str) -> str:
            c = state[name]
            return "" if c["ready"] else f'<p class="banner" role="status">{escape(c["state"])}.</p>'

        canva_state = state["canva"]
        if canva_state["ready"]:
            canva_line = ('<form method="post" action="/connectors/canva/disconnect"><span>Canva is connected to your account.</span>'
                          '<button class="quiet-button">Disconnect</button></form>')
        elif connectors.BY_NAME["canva"].missing():
            canva_line = f'<p class="banner" role="status">{escape(canva_state["state"])}.</p>'
        else:
            canva_line = (f'<p class="banner" role="status">The Canva keys are saved. Sign in once so the agent can work in your account. '
                          f'In Canva\'s developer portal the redirect URL must be <code>{escape(redirect_uri(request))}</code>.</p>'
                          f'<a class="button approve" href="/connectors/canva/connect">{icon("arrow")}Sign in to Canva</a>')
        design_options = "".join(
            f'<option value="{escape(d)}">{escape(t)} · {escape(c)}</option>' for d, t, c in designs
        )
        waiting = any(r["id"] == wait and r["status"] in ("queued", "running") for r in runs)
        return render("Studio", "work", f"""
<p class="crumb"><a href="/work">Work</a> / Studio</p>
<h1>Studio</h1>
<p class="lede">Make an image or a video with Higgsfield, or a design in your own Canva account. Each result is saved to the
 workstream's folder or stays in your Canva. Your prompt goes to Higgsfield; a title and the image you pick go to Canva.</p>
{f'<div class="banner" role="status">{escape(notice)}</div>' if notice else ""}
{ws_api.ws_chips("/studio", ws)}
<div class="columns two">
<section class="card" id="image">
  <h2>Generate an image</h2>
  {gate("higgsfield")}
  <form method="post" action="/studio" class="stack">
    <input type="hidden" name="tool" value="higgsfield_generate_image"/>
    <label>Workstream <select name="workstream" required>{streams}</select></label>
    <label>Describe the image <textarea name="prompt" rows="3" maxlength="4000" required></textarea></label>
    <button class="approve">Generate this image</button>
  </form>
</section>
<section class="card" id="video">
  <h2>Generate a video</h2>
  {gate("higgsfield")}
  <form method="post" action="/studio" class="stack">
    <input type="hidden" name="tool" value="higgsfield_generate_video"/>
    <label>Workstream <select name="workstream" required>{streams}</select></label>
    <label>Describe the video <textarea name="prompt" rows="3" maxlength="4000" required></textarea></label>
    <div class="row two">
      <label>Seconds <input type="number" name="duration" min="2" max="15" value="5"/></label>
      <label>Shape <select name="aspect_ratio"><option value="16:9">Landscape 16:9</option><option value="9:16">Portrait 9:16</option>
        <option value="1:1">Square 1:1</option></select></label>
    </div>
    <button class="approve">Generate this video</button>
  </form>
</section>
<section class="card" id="canva">
  <h2>Make a Canva design</h2>
  {canva_line}
  <form method="post" action="/studio" class="stack">
    <input type="hidden" name="tool" value="canva_create_design"/>
    <label>Workstream <select name="workstream" required>{streams}</select></label>
    <label>Title <input type="text" name="title" maxlength="255" required/></label>
    <label>Size <select name="size">{"".join(f'<option value="{k}">{escape(v[0])}</option>' for k, v in connectors.CANVA_SIZES.items())}</select></label>
    {f'<label>Place an image from the {escape(ws)} folder <span class="or">optional; it is uploaded to your Canva</span><select name="image_path"><option value="">No image</option>{images}</select></label>' if images else ""}
    <button class="approve">Make this design</button>
  </form>
</section>
<section class="card" id="export">
  <h2>Bring a Canva design back</h2>
  <p class="quiet">Saves the design to the workstream's folder as a PDF or as images, one for each page.</p>
  <form method="post" action="/studio" class="stack">
    <input type="hidden" name="tool" value="canva_export_design"/>
    <label>Workstream <select name="workstream" required>{streams}</select></label>
    {f'<label>A design made here <select name="design_pick"><option value="">Choose one</option>{design_options}</select></label>' if design_options else ""}
    <label>{"Or a" if design_options else "The"} design's ID <span class="or">the code after /design/ in its Canva address</span>
      <input type="text" name="design_id" maxlength="40" autocomplete="off"/></label>
    <label>As <select name="format"><option value="pdf">PDF</option><option value="png">PNG images</option><option value="jpg">JPG images</option></select></label>
    <button class="approve">Save it to the folder</button>
  </form>
</section>
</div>
{"<h2>Made here</h2>" + "".join(card(r) for r in runs) if runs else ""}
""", refresh=5 if waiting else 0)

    @app.post("/studio")
    async def make(request: Request, background: BackgroundTasks):
        form = await request.form()
        text = lambda key, limit=4000: str(form.get(key) or "").strip()[:limit]  # noqa: E731
        tool = text("tool", 60)
        if tool not in studio.TOOLS:
            raise HTTPException(400, "that is not something the Studio makes")
        ws = ws_api.known(text("workstream", 80))
        if not ws:
            return notice("Choose the workstream this is for.")
        if tool.startswith("higgsfield"):
            if not text("prompt"):
                return notice("Describe what to make.")
            params: dict = {"prompt": text("prompt")}
            if tool.endswith("video"):
                params.update(duration=text("duration", 3) or "5", aspect_ratio=text("aspect_ratio", 5))
        elif tool == "canva_create_design":
            if not text("title", 255):
                return notice("Give the design a title.")
            params = {"title": text("title", 255), "size": text("size", 20), "image_path": text("image_path", 300) or None}
        else:
            design_id = text("design_id", 40) or text("design_pick", 40)
            if not design_id:
                return notice("Choose a design, or give its ID.")
            params = {"design_id": design_id, "format": text("format", 5)}
        try:
            run_id = engine.start_run(studio.WORKFLOW, ws, {"tool": tool, "params": params}, work=False)
        except MonthlyCapReached as e:
            return notice(str(e))
        background.add_task(engine.work, run_id)
        return RedirectResponse(f"/studio?wait={run_id}#s-{run_id}", status_code=303)
