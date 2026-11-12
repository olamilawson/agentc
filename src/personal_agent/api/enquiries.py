"""Client enquiries: the public request page, the private brief page a link opens,
and the owner's screen where requests are decided and links are made.

The two public pages need no session. Neither calls a model: a request is
stored for the owner, and a brief starts an evaluation only through a live
link, within the month's allowance (engine/enquiries.py holds the rules).
Every state of a request, a link and a brief has its own words on the page and
a data-state attribute, so a person and a script can both tell where things stand.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from personal_agent.api import ui
from personal_agent.engine import enquiries as enq

PUBLIC_PATHS = ("/enquire", "/enquire/sent")
OTHER_TOPIC = "Something else"


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith("/brief/")


FORM_SCRIPT = """<script>
(function () {
  var form = document.querySelector('form.pform');
  if (!form) return;
  var steps = [].slice.call(form.querySelectorAll('.step'));
  var rules = {
    email: function (v) { return /^[^@\\s<>,;]+@[^@\\s<>,;]+\\.[^@\\s<>,;]+$/.test(v) ? '' : 'Add an email address, such as name@company.com.'; },
    phone: function (v) { return (v.replace(/[^0-9]/g, '').length >= 7 && /^\\+?[0-9 ()\\-]{7,24}$/.test(v)) ? '' : 'Add a phone number with its country code, such as +234 803 000 0000.'; }
  };
  function check(field) {
    var box = document.getElementById(field.name + '-error');
    var v = field.value.trim(), say = '';
    if (field.type === 'checkbox') { say = field.required && !field.checked ? field.dataset.need : ''; }
    else if (field.required && !v) { say = field.dataset.need || 'This is needed.'; }
    else if (v && rules[field.name]) { say = rules[field.name](v); }
    else if (v && field.minLength > 0 && v.length < field.minLength) { say = field.dataset.short || field.dataset.need; }
    field.setAttribute('aria-invalid', say ? 'true' : 'false');
    if (box) box.textContent = say;
    return !say;
  }
  function valid(scope) {
    var first = null;
    [].forEach.call(scope.querySelectorAll('input[name], textarea[name], select[name]'), function (f) {
      if (f.type === 'file' || f.type === 'radio' || f.closest('.trap')) return;
      if (!check(f) && !first) first = f;
    });
    if (first) first.focus();
    return !first;
  }
  form.addEventListener('blur', function (e) {
    var f = e.target;
    if (f.name && f.type !== 'checkbox' && (f.value.trim() || f.getAttribute('aria-invalid') === 'true')) check(f);
  }, true);
  form.addEventListener('input', function (e) {
    var f = e.target;
    if (f.getAttribute('aria-invalid') === 'true') check(f);
    var counter = f.id && document.querySelector('[data-count="' + f.id + '"]');
    if (counter) {
      var left = f.maxLength - f.value.length;
      counter.textContent = left + ' characters left';
      counter.dataset.state = left < 100 ? 'near' : 'ok';
    }
  });
  function show(n) {
    steps.forEach(function (s, i) { s.hidden = i !== n; });
    // Each list of steps (the bar above the form, the list beside it) is numbered on its own.
    ['.stepper li', '.journey li'].forEach(function (list) {
      [].forEach.call(document.querySelectorAll(list), function (m, i) { m.dataset.state = i < n ? 'done' : i === n ? 'current' : 'next'; });
    });
    form.dataset.step = String(n + 1);
    var head = steps[n] && steps[n].querySelector('h2, .eyebrow');
    if (head) { head.setAttribute('tabindex', '-1'); head.focus({preventScroll: true}); }
    window.scrollTo({top: 0, behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
  }
  if (steps.length > 1) {
    show(Math.max(0, Math.min(steps.length - 1, (parseInt(form.dataset.step, 10) || 1) - 1)));
    form.addEventListener('click', function (e) {
      var go = e.target.closest('[data-go]');
      if (!go) return;
      var n = parseInt(go.dataset.go, 10) - 1;
      if (n > steps.findIndex(function (s) { return !s.hidden; }) && !valid(steps[n - 1])) return;
      show(n);
    });
  }
  var drop = form.querySelector('.drop'), input = drop && drop.querySelector('input[type="file"]'), list = form.querySelector('.files');
  var kept = [];
  function draw() {
    if (!list) return;
    var size = function (b) { return b > 999999 ? (b / 1e6).toFixed(1) + ' MB' : Math.max(1, Math.round(b / 1e3)) + ' KB'; };
    list.innerHTML = '';
    kept.forEach(function (file, i) {
      var li = document.createElement('li'), name = document.createElement('span'), small = document.createElement('small');
      var off = document.createElement('button');
      name.textContent = file.name; small.textContent = size(file.size); name.appendChild(small);
      off.type = 'button'; off.setAttribute('aria-label', 'Remove ' + file.name); off.innerHTML = '__X__';
      off.addEventListener('click', function () { kept.splice(i, 1); sync(); });
      li.appendChild(name); li.appendChild(off); list.appendChild(li);
    });
    drop.querySelector('[data-files]').textContent = kept.length ? kept.length + (kept.length === 1 ? ' file ready' : ' files ready') : '';
  }
  function sync() {
    try { var t = new DataTransfer(); kept.forEach(function (f) { t.items.add(f); }); input.files = t.files; } catch (e) {}
    draw();
  }
  if (drop && input) {
    input.addEventListener('change', function () { kept = kept.concat([].slice.call(input.files)).slice(0, 5); sync(); });
    ['dragenter', 'dragover'].forEach(function (k) { drop.addEventListener(k, function (e) { e.preventDefault(); drop.dataset.state = 'over'; }); });
    ['dragleave', 'drop'].forEach(function (k) { drop.addEventListener(k, function (e) { e.preventDefault(); drop.dataset.state = 'idle'; }); });
    drop.addEventListener('drop', function (e) { kept = kept.concat([].slice.call(e.dataTransfer.files)).slice(0, 5); sync(); });
  }
  form.addEventListener('submit', function (e) {
    if (form.dataset.state === 'sending') { e.preventDefault(); return; }
    if (!valid(form)) {
      e.preventDefault();
      var bad = form.querySelector('[aria-invalid="true"]'), at = bad && bad.closest('.step');
      if (at && at.hidden) { show(steps.indexOf(at)); bad.focus(); }
      return;
    }
    form.dataset.state = 'sending';
    var b = form.querySelector('button[type="submit"]');
    if (b) { b.setAttribute('aria-disabled', 'true'); b.querySelector('[data-label]').textContent = b.dataset.sending; }
  });
})();
</script>""".replace("__X__", ui.icon("x"))

COPY_SCRIPT = """<script>
(function () {
  [].forEach.call(document.querySelectorAll('[data-copy]'), function (b) {
    b.hidden = false;
    b.addEventListener('click', function () {
      var text = document.getElementById(b.dataset.copy).textContent.trim();
      navigator.clipboard.writeText(text).then(function () {
        var was = b.textContent; b.textContent = 'Copied'; b.dataset.state = 'copied';
        setTimeout(function () { b.textContent = was; b.dataset.state = 'idle'; }, 2000);
      });
    });
  });
})();
</script>"""


def install(app: FastAPI, *, settings, db, engine, render, ws_api, dock) -> None:
    icon = ui.icon
    policy = settings.enquiries
    zone = ZoneInfo(settings.calendar.timezone)

    def day(stamp: str | None) -> str:
        if not stamp:
            return ""
        when = datetime.fromisoformat(stamp).astimezone(zone)
        return f"{when.day} {when:%B %Y}"

    def when(stamp: str | None) -> str:
        if not stamp:
            return ""
        moment = datetime.fromisoformat(stamp).astimezone(zone)
        return f"{moment.day} {moment:%b}, {moment:%H:%M}"

    def link_for(request: Request, token: str) -> str:
        base = (settings.public_url or str(request.base_url)).rstrip("/")
        return f"{base}/brief/{token}"

    def same_site(request: Request) -> bool:
        """A public form may be sent only from this site's own page. The brief page sends no referrer,
        so its browser sends Origin "null"; Sec-Fetch-Site still says where the form came from."""
        fetched_from = request.headers.get("sec-fetch-site")
        if fetched_from and fetched_from not in ("same-origin", "none"):
            return False
        origin = request.headers.get("origin")
        return not origin or origin == "null" or urlsplit(origin).netloc == request.headers.get("host")

    def network_address(request: Request) -> str:
        # nginx appends the address it saw as the last X-Forwarded-For entry; the app is reachable only through it.
        forwarded = request.headers.get("x-forwarded-for", "")
        return forwarded.split(",")[-1].strip() if forwarded else (request.client.host if request.client else "")

    # --- pieces of the public pages ---------------------------------------------------------------
    # What the twin works on, drawn around her on the request page.
    PILLS = [("layers", "Brand &amp;", "Growth"), ("megaphone", "Communications", "&amp; Reputation"),
             ("laptop", "Innovation", "&amp; Digital"), ("draft", "Writing &amp;", "Thought Leadership")]

    def brand() -> str:
        return (f'<a class="pub-brand" href="https://chidimma.com/ijeoma-pro/" aria-label="{escape(policy.name)}, by '
                f'{escape(policy.owner_full_name)}"><span class="ij" aria-hidden="true">ij<i>.</i></span>'
                f'<span class="pub-name" aria-hidden="true">{escape(policy.name)}<small>By {escape(policy.owner_full_name)}</small></span></a>')

    def headline(words: str) -> str:
        """A headline with the brand's full stop in orange."""
        return f'{escape(words.rstrip("."))}<span class="fullstop">.</span>'

    def twin() -> str:
        pills = "".join(
            f'<span class="pill p{i}">{icon(glyph)}<span>{top}<br/>{bottom}</span></span>'
            for i, (glyph, top, bottom) in enumerate(PILLS, start=1)
        )
        return (f'<div class="twin" aria-hidden="true">{ui.SPARK}<img src="/static/brand/twin.webp" alt="" width="410" height="480"/>'
                f'{pills}</div>')

    def journey(items: list[tuple[str, str]], current: int) -> str:
        return '<ol class="journey">' + "".join(
            f'<li data-step-mark data-state="{"done" if i < current else "current" if i == current else "next"}">'
            f'<span><b>{escape(title)}</b>{escape(text)}</span></li>'
            for i, (title, text) in enumerate(items)
        ) + "</ol>"

    def hero(title: str, sub: str, extra: str = "") -> str:
        return f'<h1>{headline(title)}</h1><p class="sub">{escape(sub)}</p>{extra}'

    def links() -> str:
        privacy = (f'<a href="{escape(policy.privacy_url)}" target="_blank" rel="noopener">Privacy Policy</a>'
                   if policy.privacy_url else "Privacy Policy")
        if policy.terms_url:
            return (f'agree to the <a href="{escape(policy.terms_url)}" target="_blank" rel="noopener">Terms of Use</a> '
                    f'and acknowledge the {privacy}')
        return f"acknowledge the {privacy}"

    def assure(action: str = "continuing") -> str:
        # Sending the form is the agreement, as the owner's design has it; terms_at records when.
        return (f'<div class="assure">{icon("shield")}<span class="rule" aria-hidden="true"></span>'
                f'<p>By {action}, you {links()}.</p></div>')

    def field(name: str, label: str, values: dict, wrong: dict, *, kind: str = "text", need: str = "", hint: str = "",
              placeholder: str = "", required: bool = True, wide: bool = False, rows: int = 0, limit: int = 0,
              minimum: int = 0, auto: str = "", optional_note: bool = False) -> str:
        value, error = values.get(name, ""), wrong.get(name, "")
        limit = limit or enq.LIMITS.get(name) or enq.BRIEF_FIELDS.get(name) or 200
        attrs = [f'id="f-{name}"', f'name="{name}"', f'maxlength="{limit}"', f'data-need="{escape(need)}"',
                 f'aria-describedby="{name}-error"', f'aria-invalid="{"true" if error else "false"}"']
        attrs += ["required"] if required else []
        attrs += [f'minlength="{minimum}"'] if minimum else []
        attrs += [f'placeholder="{escape(placeholder)}"'] if placeholder else []
        attrs += [f'autocomplete="{auto}"'] if auto else []
        common = " ".join(attrs)
        control = (f'<textarea {common} rows="{rows}">{escape(value)}</textarea>'
                   f'<span class="counter" data-count="f-{name}" aria-live="polite"></span>' if rows
                   else f'<input type="{kind}" {common} value="{escape(value)}"/>')
        star = '<b class="req" aria-hidden="true">*</b>' if required else ""
        note = '<span class="opt">(optional)</span>' if optional_note else ""
        aside = f' <span class="hint">{escape(hint)}</span>' if hint else ""
        # The message sits outside the label, so it is read as a description, not as part of the field's name.
        return (f'<div class="field{" wide" if wide else ""}"><label for="f-{name}">{escape(label)}{star}{note}{aside}</label>{control}'
                f'<p class="field-error" id="{name}-error" role="alert">{escape(error)}</p></div>')

    def person_fields(values: dict, wrong: dict) -> str:
        f = lambda *a, **k: field(*a, values=values, wrong=wrong, **k)  # noqa: E731
        return ('<div class="grid">'
                + f("first_name", "First name", need="Add your first name.", placeholder="Your first name", auto="given-name")
                + f("last_name", "Last name", need="Add your last name.", placeholder="Your last name", auto="family-name")
                + f("email", "Email address", kind="email", need="Add an email address, such as name@company.com.",
                    placeholder="you@example.com", auto="email", wide=True)
                + f("organisation", "Organisation or business", placeholder="Where you work", auto="organization",
                    required=False, optional_note=True)
                + f("role", "Role or profession", placeholder="Your role or focus area", auto="organization-title",
                    required=False, optional_note=True)
                + "</div>")

    def marketing(values: dict) -> str:
        return (f'<label class="consent"><input type="checkbox" name="marketing"{" checked" if values.get("marketing") else ""}/>'
                f'<span>I&#8217;d like to receive insights, updates and opportunities from {escape(policy.owner_full_name)} '
                f'and her ventures.</span></label>')

    def trap() -> str:
        # A field people never see. A program that fills every field fills this one, and its request is dropped.
        return ('<div class="trap" aria-hidden="true"><label>Leave this empty <input type="text" name="website" '
                'tabindex="-1" autocomplete="off"/></label></div>')

    def submit(label: str, sending: str) -> str:
        return (f'<button type="submit" class="go" data-sending="{escape(sending)}"><span data-label>{escape(label)}</span>'
                f'<span class="spinner" aria-hidden="true"></span>{icon("arrow")}</button>')

    def page(title: str, body: str, hero_html: str, *, script: str = "", private: bool = False, flat: bool = False,
             status: int = 200) -> HTMLResponse:
        return HTMLResponse(ui.public_page(title, body, hero=hero_html, brand=brand(), script=script, private=private, flat=flat),
                            status_code=status)

    # --- the public request page ------------------------------------------------------------------
    HERO_SUB = "A conversational, AI-powered extension of my thinking, experience and expertise."

    def topic_of(value: str) -> str:
        if value.isdigit() and 1 <= int(value) <= len(policy.topics):
            return policy.topics[int(value) - 1]
        return value if value in policy.topics or value == OTHER_TOPIC else ""

    def request_page(values: dict, wrong: dict, notice: str = "", status: int = 200) -> HTMLResponse:
        closed = db.count_enquiries(status="received") >= policy.waiting_cap
        intro = (f'<p class="kicker">Get started</p><h2 class="card-title">{headline("Let’s get you in.")}</h2>'
                 f'<p class="card-lede">Request access to my digital twin. I read every request and send you a private link.</p>')
        if closed:
            body = (f'{intro}<div class="notice" role="status" data-state="closed">{icon("clock")}<span>Requests are closed for now '
                    f'while I catch up. Please try again in a few days.</span></div>')
        else:
            body = f"""{intro}
{f'<div class="notice stop" role="alert">{icon("risk")}<span>{escape(notice)}</span></div>' if notice else ""}
<form class="pform" method="post" action="/enquire" novalidate data-state="{"error" if wrong or notice else "open"}">
  <input type="hidden" name="topic" value="{escape(values.get("topic", ""))}"/>
  {person_fields(values, wrong)}
  {marketing(values)}
  {trap()}
  {submit("Request access", "Sending your request")}
</form>
<p class="after">Already have a private link? Open it from the message I sent you.</p>
{assure()}"""
        return page(f"Request access · {policy.name}", body, hero("Explore my digital twin.", HERO_SUB, twin()),
                    script=FORM_SCRIPT, status=status)

    @app.get("/enquire", response_class=HTMLResponse)
    def enquire_page(topic: str = ""):
        return request_page({"topic": topic_of(topic)}, {})

    @app.post("/enquire")
    async def enquire(request: Request):
        if not same_site(request):
            return PlainTextResponse("This form can be sent only from its own page.", status_code=403)
        form = await request.form()
        if str(form.get("website") or "").strip():
            return RedirectResponse("/enquire/sent", status_code=303)  # the hidden field was filled: dropped, without saying so
        fields = enq.clean(form)
        values = {**fields, "topic": topic_of(str(form.get("topic") or "")), "marketing": form.get("marketing") == "on"}
        wrong = enq.check_person(fields)
        if wrong:
            return request_page(values, wrong, status=400)
        key = enq.address_key(network_address(request), settings.session_secret or "a machine with no session secret")
        try:
            enq.receive(db, policy, fields, values["topic"], key, values["marketing"])
        except enq.Refused as e:
            return request_page(values, {}, str(e), status=429)
        return RedirectResponse("/enquire/sent", status_code=303)

    @app.get("/enquire/sent", response_class=HTMLResponse)
    def enquiry_sent():
        body = f"""
<div class="outcome" data-state="received">
  <div class="seal">{icon("check")}</div>
  <h2 class="card-title">{headline("Thank you. It’s with me.")}</h2>
  <p>I read every request myself. If the work fits, I will send you a private link to share your brief with {escape(policy.name)}.</p>
  <ul class="next">
    <li>{icon("mail")}<span>Watch for a message from me at the email address you gave.</span></li>
    <li>{icon("clock")}<span>Each private link works for {policy.invite_days} days from the day I send it.</span></li>
  </ul>
  <a class="go" href="https://chidimma.com/">Back to chidimma.com {icon("arrow")}</a>
</div>"""
        return page(f"Request sent · {policy.name}", body, hero("Explore my digital twin.", HERO_SUB, twin()))

    # --- the private brief page ---------------------------------------------------------------------
    BRIEF_STEPS = [("Your details", "Check them before you share the brief."),
                   ("Your brief", "What the work should achieve, and any files."),
                   ("Sent", "I review everything before I reply.")]

    def brief_hero(e: dict, current: int) -> str:
        return hero("Share your brief.", f"This link is for {e['first_name']}. It works until {day(e['expires_at'])}.",
                    journey(BRIEF_STEPS, current))

    def gone_page(state: str, e: dict | None) -> HTMLResponse:
        words = {
            "unknown": ("This link does not open a brief.", "Check that you copied the whole link from my message. "
                        "If it still does not open, request access again.", 404),
            "expired": ("This link has expired.", f"It worked until {day(e and e['expires_at'])}. Request access again "
                        "and I will send you a new one.", 410),
            "cancelled": ("This link is no longer active.", "Request access again if you still want to send a brief.", 410),
            "briefed": ("Your brief reached me.", "This link has done its job, so it opens no more forms. "
                        "I will reply at the email address you gave.", 200),
        }[state]
        body = (f'<div class="outcome" data-state="{state}"><div class="seal{"" if state == "briefed" else " wait"}">'
                f'{icon("check" if state == "briefed" else "link")}</div><h2 class="card-title">{headline(words[0])}</h2>'
                f'<p>{escape(words[1])}</p>'
                f'{"" if state == "briefed" else f"""<a class="go" href="/enquire">Request access {icon("arrow")}</a>"""}</div>')
        return page(f"Private brief · {policy.name}", body,
                    hero("Share your brief.", f"A private link from {policy.owner_full_name}."),
                    private=True, flat=True, status=words[2])

    def brief_page(token: str, e: dict, values: dict, wrong: dict, step: int, notice: str = "", status: int = 200) -> HTMLResponse:
        f = lambda *a, **k: field(*a, values=values, wrong=wrong, **k)  # noqa: E731
        left = e["briefs_left"]
        body = f"""
<ol class="stepper" aria-label="Progress"><li data-step-mark data-state="current">1. Your details</li><li data-step-mark data-state="next">2. Your brief</li></ol>
{f'<div class="notice stop" role="alert">{icon("risk")}<span>{escape(notice)}</span></div>' if notice else ""}
<form class="pform" method="post" action="/brief/{escape(token)}" enctype="multipart/form-data" novalidate data-state="{"error" if wrong or notice else "open"}" data-step="{step}">
  <div class="step" data-step-name="details">
    <h2>Your details</h2>
    <p class="intro">Check these before you share the brief. I reply to the email address here.</p>
    {person_fields(values, wrong)}
    {"" if e["marketing_at"] else marketing(values)}
    {assure("sending your brief") if not e["terms_at"] else ""}
    <div class="step-nav gap-top"><span class="meta">This link accepts {left} brief{"s" if left != 1 else ""} until {escape(day(e["expires_at"]))}.</span>
      <button type="button" class="go" data-go="2">Continue {icon("arrow")}</button></div>
  </div>
  <div class="step" data-step-name="brief">
    <h2>Your brief</h2>
    <p class="intro">Share what you know now. Gaps are fine: I come back with questions.</p>
    <section class="group"><div class="grid">
      {f("project", "Project name", need="Give the project a name.", placeholder="For example, Spring launch", wide=True)}
      {f("objective", "What should this work achieve?", rows=4, wide=True, minimum=20,
         need="Say what the work should achieve, in a sentence or two.",
         hint="The business result you want, and how you will know it worked.")}
      {f("audience", "Who is it for?", rows=3, wide=True, required=False, hint="The people it has to reach or persuade.")}
      {f("deliverables", "What do you need delivered?", rows=3, wide=True, required=False,
         hint="For example a strategy, a campaign, a review of existing work.")}
      {f("budget", "Budget", required=False, hint="A range is enough.")}
      {f("dates", "Key dates", required=False, hint="Deadlines, launches, events.")}
      {f("notes", "Anything else I should know", rows=3, wide=True, required=False)}
    </div></section>
    <section class="group">
      <label class="drop" data-state="idle">{icon("upload")}<b>Attach your brief or supporting files</b>
        <span>Drop them here, or choose files. PDF, Word, PowerPoint, text or images; up to {enq.MAX_FILES} files, 10 MB each.</span>
        <span data-files aria-live="polite"></span>
        <input type="file" name="files" multiple accept=".pdf,.docx,.pptx,.txt,.md,.png,.jpg,.jpeg,.webp"/></label>
      <ul class="files"></ul>
      <p class="meta">{escape(policy.name)} reads PDF, Word and text files. Images and slides come to me as they are.</p>
    </section>
    <div class="step-nav"><button type="button" class="button" data-go="1">{icon("back")} Back</button>
      {submit("Send my brief", "Sending your brief")}</div>
  </div>
  {trap()}
</form>"""
        return page(f"Share your brief · {policy.name}", body, brief_hero(e, step - 1), script=FORM_SCRIPT, private=True,
                    flat=True, status=status)

    def person_values(e: dict) -> dict:
        return {k: e.get(k) or "" for k in enq.LIMITS if k not in ("message", "phone")}

    @app.get("/brief/{token}", response_class=HTMLResponse)
    def open_brief(token: str):
        state, e = enq.link_state(db, token)
        if state != "live":
            return gone_page(state, e)
        return brief_page(token, e, person_values(e), {}, 1)

    @app.post("/brief/{token}")
    async def send_brief(token: str, request: Request, background: BackgroundTasks):
        if not same_site(request):
            return PlainTextResponse("This form can be sent only from its own page.", status_code=403)
        state, e = enq.link_state(db, token)
        if state != "live":
            return gone_page(state, e)
        form = await request.form(max_files=enq.MAX_FILES + 1, max_fields=60)
        if str(form.get("website") or "").strip():
            return RedirectResponse(f"/brief/{token}/sent", status_code=303)
        person = {k: v for k, v in enq.clean(form).items() if k not in ("message", "phone")}
        brief = {k: str(form.get(k) or "").strip()[:n].replace("\r\n", "\n") for k, n in enq.BRIEF_FIELDS.items()}
        values = {**person, **brief, "marketing": form.get("marketing") == "on"}
        wrong = enq.check_person(person)
        brief_wrong = enq.check_brief(brief)
        if wrong or brief_wrong:
            return brief_page(token, e, values, {**wrong, **brief_wrong}, 1 if wrong else 2, status=400)
        files = []
        for upload in form.getlist("files"):
            if getattr(upload, "filename", ""):
                files.append((upload.filename, await upload.read(enq.MAX_FILE_BYTES + 1)))
        problem = enq.check_files(files)
        if problem:
            return brief_page(token, e, values, {}, 2, notice=problem, status=400)
        try:
            entry = enq.take_brief(engine, e, person, brief, files, values["marketing"])
        except enq.Refused:
            return gone_page(*enq.link_state(db, token))
        if entry["run_id"]:
            background.add_task(engine.work, entry["run_id"])
        return RedirectResponse(f"/brief/{token}/sent", status_code=303)

    @app.get("/brief/{token}/sent", response_class=HTMLResponse)
    def brief_sent(token: str):
        state, e = enq.link_state(db, token)
        if e is None or not e["briefs"]:
            return gone_page(state, e)
        latest = e["briefs"][-1]
        more = state == "live" and e["briefs_left"] > 0
        reading = (f"{escape(policy.name)} is reading it now and preparing notes for me. I review everything before I reply."
                   if latest.get("run_id") else "I will read it and reply.")
        body = f"""
<div class="outcome" data-state="sent">
  <div class="seal">{icon("check")}</div>
  <h2 class="card-title">{headline(f"Your brief is in, {e['first_name']}.")}</h2>
  <p>{reading}</p>
  <ul class="next">
    <li>{icon("mail")}<span>I reply to you at {escape(e["email"])}.</span></li>
    {f'<li>{icon("link")}<span>This link accepts {e["briefs_left"]} more brief{"s" if e["briefs_left"] != 1 else ""} until {escape(day(e["expires_at"]))}. <a href="/brief/{escape(token)}">Send another brief</a></span></li>' if more else ""}
  </ul>
</div>"""
        return page(f"Brief sent · {policy.name}", body, brief_hero(e, 2), private=True, flat=True)

    # --- the owner's screen -------------------------------------------------------------------------
    TABS = [("", "All"), ("received", "Waiting for you"), ("invited", "Links live"), ("briefed", "Briefs in"),
            ("closed", "Closed")]

    def owned(enquiry_id: str) -> dict:
        enq.settle(db)
        e = db.get_enquiry(enquiry_id)
        if not e:
            raise HTTPException(404, "no such enquiry")
        return e

    def name_of(e: dict) -> str:
        return f"{e['first_name']} {e['last_name'] or ''}".strip()

    def state_chip(e: dict) -> str:
        return ws_api.chip(enq.STATES.get(e["status"], (e["status"], "c-slate")))

    def run_chip(run_id: str | None) -> str:
        run = db.get_run(run_id) if run_id else None
        if not run:
            return ws_api.chip(("Waiting for you to start it", "c-amber"))
        return ws_api.chip({
            "queued": ("Queued", "c-slate"), "running": ("Evaluating", "c-blue"), "done": ("Evaluated", "c-emerald"),
            "waiting_for_approval": ("Ready for your review", "c-amber"), "waiting_for_input": ("Needs your answer", "c-amber"),
            "failed": ("Evaluation failed", "c-rose"), "cancelled": ("Cancelled", "c-slate"),
        }.get(run["status"], (run["status"], "c-slate")))

    def stream_choice(default: str) -> str:
        names = engine.harness.clients.list_clients()
        options = "".join(f'<option value="{escape(n)}"{" selected" if n == default else ""}>{escape(n)}</option>' for n in names)
        return (f'<div class="row two"><label>Workstream <select name="workstream"><option value="">A new workstream</option>{options}</select></label>'
                f'<label><span class="or">or a new workstream\'s name</span><input type="text" name="workstream_new" maxlength="80" '
                f'value="{"" if default in names else escape(default)}"/></label></div>')

    def link_terms() -> str:
        return (f'<div class="row two"><label>The link works for, in days <input type="number" name="days" min="1" max="30" '
                f'value="{policy.invite_days}"/></label><label>Briefs it accepts <input type="number" name="briefs" min="1" max="5" '
                f'value="{policy.briefs_per_invite}"/></label></div>')

    def chosen_stream(form) -> str | None:
        name = str(form.get("workstream_new") or "").strip() or str(form.get("workstream") or "").strip()
        if not name:
            return None
        try:
            return engine.harness.clients.ensure(name).name
        except Exception:
            return None

    def number(form, key: str) -> int | None:
        try:
            return int(str(form.get(key) or ""))
        except ValueError:
            return None

    def back(path: str, notice: str) -> RedirectResponse:
        return RedirectResponse(f"{path}?notice={quote(notice)}", status_code=303)

    @app.get("/enquiries", response_class=HTMLResponse)
    def enquiries_page(request: Request, status: str = "", notice: str = "") -> str:
        enq.settle(db)
        everything = db.list_enquiries()
        shown = [e for e in everything if not status or e["status"] == status
                 or (status == "closed" and e["status"] in enq.SETTLED)]
        count = lambda s: sum(1 for e in everything if e["status"] == s)  # noqa: E731
        rows = "".join(
            f'<li data-state="{e["status"]}"><a href="/enquiries/{e["id"]}"><span class="lead">{ui.stream_badge(e["organisation"] or e["first_name"])}'
            f'<span><b>{escape(name_of(e))}</b><small>{escape(" · ".join(x for x in (e["role"], e["organisation"], e["topic"]) if x))}'
            f'{" · you invited them" if e["origin"] == "owner" else ""}</small></span></span>'
            f'<span class="end">{state_chip(e)}<span class="due">{escape(when(e["updated_at"]))}</span>{icon("chevron")}</span></a></li>'
            for e in shown
        )
        tabs = '<nav class="tabs" aria-label="Show">' + "".join(
            f'<a href="/enquiries{f"?status={key}" if key else ""}"{" aria-current=true" if key == status else ""}>{label}</a>'
            for key, label in TABS
        ) + "</nav>"
        public = f"{(settings.public_url or str(request.base_url)).rstrip('/')}/enquire"
        used = enq.evaluations_this_month(db)
        return render("Client enquiries", "enquiries", f"""
<p class="crumb"><a href="/work">Work</a> / Client enquiries</p>
<h1>Client enquiries</h1>
<p class="lede">Requests from your public page, and the private links that let a person send you a brief. A request costs
 nothing. Only a brief sent through a link starts an evaluation, and {policy.evaluations_per_month} a month start by themselves.</p>
{f'<div class="banner" role="status">{escape(notice)}</div>' if notice else ""}
<div class="strip" data-state="summary">
  <a href="/enquiries?status=received"><strong>{count("received")}</strong>Waiting for your decision</a>
  <a href="/enquiries?status=invited"><strong>{count("invited")}</strong>Links live</a>
  <a href="/enquiries?status=briefed"><strong>{used} of {policy.evaluations_per_month}</strong>Briefs evaluated this month</a>
</div>
<section class="card"><div class="linkrow"><h2>Your public page</h2></div>
  <p>Anyone can request access here. Link to it from chidimma.com; <code>?topic=1</code> to <code>?topic={len(policy.topics)}</code>
   picks a topic in advance.</p>
  <p class="actions"><code id="public-link">{escape(public)}</code><button type="button" data-copy="public-link" hidden>Copy the address</button>
   <a class="button" href="/enquire" target="_blank" rel="noopener">Open it</a></p>
</section>
<details class="card add"><summary>{icon("plus")}Make a link for someone you know</summary>
  <form method="post" action="/enquiries/invite" class="stack">
    <p class="meta">For a client who has not used the public page. They fill in their other details and agree to the terms on the brief form.</p>
    <div class="row two"><label>First name <input type="text" name="first_name" maxlength="80" required/></label>
      <label>Last name <input type="text" name="last_name" maxlength="80"/></label></div>
    <div class="row two"><label>Email address <input type="email" name="email" maxlength="254" required/></label>
      <label>Organisation <input type="text" name="organisation" maxlength="120"/></label></div>
    {stream_choice("")}
    {link_terms()}
    <button class="approve">Make the link</button>
  </form></details>
{tabs}
<section class="card">
  {f'<ul class="rows">{rows}</ul>' if rows else '<p class="quiet">Nothing here yet. Requests from your public page arrive here.</p>'}
</section>
""", script=COPY_SCRIPT, dock=dock())

    @app.post("/enquiries/invite")
    async def invite_someone(request: Request):
        form = await request.form()
        fields = {k: v for k, v in enq.clean(form).items() if k != "message"}
        if not fields["first_name"] or not enq.ADDRESS.fullmatch(fields["email"]):
            return back("/enquiries", "A link needs the person's first name and email address.")
        stream = chosen_stream(form) or chosen_stream({"workstream_new": fields["organisation"] or fields["first_name"]})
        if not stream:
            return back("/enquiries", "Choose a workstream, or give a new one a name.")
        enquiry_id = enq.invite_direct(db, policy, fields, stream, number(form, "days"), number(form, "briefs"))
        return RedirectResponse(f"/enquiries/{enquiry_id}?notice={quote('The link is live. Copy it, or prepare an email with it.')}",
                                status_code=303)

    @app.get("/enquiries/{enquiry_id}", response_class=HTMLResponse)
    def enquiry_page(enquiry_id: str, request: Request, notice: str = "") -> str:
        e = owned(enquiry_id)
        status = e["status"]
        mail_run = db.get_run(e["email_run"]) if e["email_run"] else None
        if status == "received":
            action = f"""
<section class="card fit" data-state="deciding"><h2>Your decision</h2>
  <p>A private link lets {escape(e["first_name"])} send one brief to your agent. Nothing is sent to them until you copy the link or approve an email with it.</p>
  <form method="post" action="/enquiries/{e["id"]}/invite" class="stack">
    {stream_choice(e["organisation"] or "")}
    {link_terms()}
    <button class="approve">{icon("link")}Make a private link</button>
  </form>
  <details><summary>Decline instead</summary>
    <form method="post" action="/enquiries/{e["id"]}/decline" class="stack">
      <label>A note for yourself <span class="or">optional; it is not sent</span><input type="text" name="note" maxlength="300"/></label>
      <p class="meta">Declining sends nothing. If you want to tell them, write to them yourself.</p>
      <button class="reject">Decline this request</button>
    </form></details>
</section>"""
        elif status == "invited":
            mail = (f'<p class="meta">The email with this link: {run_chip(e["email_run"]) if mail_run else ""} '
                    f'<a href="/queue">Open Review</a></p>' if mail_run and mail_run["status"] == "waiting_for_approval" else
                    f'<p class="meta">The email with this link was {"sent" if (mail_run["outputs"] or {}).get("sent") else "kept in the outbox, since no mailbox is connected"}.</p>'
                    if mail_run and mail_run["status"] == "done" else
                    f'<form method="post" action="/enquiries/{e["id"]}/email"><button>{icon("mail")}Prepare an email with the link</button></form>')
            action = f"""
<section class="card fit" data-state="live"><h2>The private link</h2>
  <p class="actions"><code id="brief-link">{escape(link_for(request, e["token"]))}</code>
    <button type="button" data-copy="brief-link" hidden>Copy the link</button></p>
  <p class="meta">Works until {escape(day(e["expires_at"]))} · {e["briefs_left"]} brief{"s" if e["briefs_left"] != 1 else ""} left ·
   workstream {escape(e["workstream"] or "")}. Anyone holding the link can use it, so send it to {escape(e["first_name"])} alone.</p>
  <div class="actions">{mail}
    <form method="post" action="/enquiries/{e["id"]}/cancel"><button class="recall">{icon("risk")}Cancel the link</button></form></div>
</section>"""
        else:
            action = ""
        briefs = "".join(
            f'<li><div class="row-static"><span class="lead"><span class="disc sm">{icon("brief")}</span><span><b>{escape(b.get("project") or "Brief")}</b>'
            f'<small>{escape(when(b["at"]))} · {escape(b["file"])}{" · " + escape(", ".join(b["files"])) if b.get("files") else ""}</small></span></span>'
            f'<span class="end">{run_chip(b.get("run_id"))}'
            + (f'<a class="button" href="/inspect/{b["run_id"]}">Open the evaluation</a>' if b.get("run_id") else
               f'<form method="post" action="/enquiries/{e["id"]}/briefs/{i}/evaluate"><button class="approve">Evaluate now</button></form>')
            + "</span></div></li>"
            for i, b in enumerate(e["briefs"] or [])
        )
        held = any(not b.get("run_id") for b in e["briefs"] or [])
        return render("Client enquiry", "enquiries", f"""
<p class="crumb"><a href="/work">Work</a> / <a href="/enquiries">Client enquiries</a> / {escape(name_of(e))}</p>
{f'<div class="banner" role="status">{escape(notice)}</div>' if notice else ""}
<article class="sheet-page" data-state="{status}">
  <div class="sheet-strip"></div>
  <header class="sheet-head">{ui.stream_badge(e["organisation"] or e["first_name"])}
    <div class="grow"><p class="chips"><span class="tag c-purple">{"Your invite" if e["origin"] == "owner" else "Request from your public page"}</span>
      {state_chip(e)}</p><h1>{escape(name_of(e))}</h1></div></header>
  <div class="sheet-body">
    <div class="facts">
      <div><p class="eyebrow">Role and organisation</p><b>{escape(e["role"] or "Not given yet")}</b><small>{escape(e["organisation"] or "")}</small></div>
      <div><p class="eyebrow">Contact</p><b>{escape(e["email"] or "")}</b><small>{escape(e["phone"] or "")}</small></div>
      <div><p class="eyebrow">Consent</p><b>{f"Terms agreed {escape(day(e['terms_at']))}" if e["terms_at"] else "Not agreed yet"}</b>
        <small>{"Opted in to marketing" if e["marketing_at"] else "No marketing opt-in"}</small></div>
    </div>
    {f'<div class="said"><p class="eyebrow">Where they want help</p><p>{escape(e["topic"])}</p></div>' if e["topic"] else ""}
    {f'<div class="said"><p class="eyebrow">About the work, in their words</p><pre class="say">{escape(e["message"])}</pre></div>' if e["message"] else ""}
    {f'<p class="meta">Your note: {escape(e["note"])}</p>' if e["note"] else ""}
    <p class="meta">Received {escape(when(e["created_at"]))}{f" · decided {escape(when(e['decided_at']))}" if e["decided_at"] else ""}.</p>
  </div>
</article>
{action}
{f'<section class="card fit"><h2>Briefs</h2>{"<p class=meta>A brief past this month&#39;s allowance waits for you to start its evaluation.</p>" if held else ""}<ul class="rows">{briefs}</ul></section>' if briefs else ""}
""", script=COPY_SCRIPT)

    @app.post("/enquiries/{enquiry_id}/invite")
    async def invite_from_request(enquiry_id: str, request: Request):
        e = owned(enquiry_id)
        form = await request.form()
        stream = chosen_stream(form)
        if not stream:
            return back(f"/enquiries/{enquiry_id}", "Choose a workstream, or give a new one a name.")
        try:
            enq.invite(db, policy, e["id"], stream, number(form, "days"), number(form, "briefs"))
        except enq.Refused as err:
            return back(f"/enquiries/{enquiry_id}", str(err))
        return back(f"/enquiries/{enquiry_id}", "The link is live. Copy it, or prepare an email with it.")

    @app.post("/enquiries/{enquiry_id}/decline")
    async def decline_request(enquiry_id: str, request: Request):
        owned(enquiry_id)
        form = await request.form()
        try:
            enq.decline(db, enquiry_id, str(form.get("note") or "").strip()[:300])
        except enq.Refused as err:
            return back(f"/enquiries/{enquiry_id}", str(err))
        return back(f"/enquiries/{enquiry_id}", "Declined. Nothing went to them.")

    @app.post("/enquiries/{enquiry_id}/cancel")
    def cancel_link(enquiry_id: str):
        owned(enquiry_id)
        try:
            enq.cancel(db, enquiry_id)
        except enq.Refused as err:
            return back(f"/enquiries/{enquiry_id}", str(err))
        return back(f"/enquiries/{enquiry_id}", "You cancelled the link. It no longer opens the brief form.")

    @app.post("/enquiries/{enquiry_id}/email")
    def email_link(enquiry_id: str, request: Request):
        e = owned(enquiry_id)
        try:
            run_id = enq.mail_link(engine, enquiry_id, link_for(request, e["token"] or ""))
        except enq.Refused as err:
            return back(f"/enquiries/{enquiry_id}", str(err))
        return RedirectResponse(f"/queue#d-{run_id}", status_code=303)

    @app.post("/enquiries/{enquiry_id}/briefs/{index}/evaluate")
    def evaluate_brief(enquiry_id: str, index: int, background: BackgroundTasks):
        owned(enquiry_id)
        try:
            run_id = enq.evaluate_held(engine, enquiry_id, index)
        except enq.Refused as err:
            return back(f"/enquiries/{enquiry_id}", str(err))
        background.add_task(engine.work, run_id)
        return RedirectResponse(f"/inspect/{run_id}", status_code=303)
