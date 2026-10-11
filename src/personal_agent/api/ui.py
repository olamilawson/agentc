"""The shared page shell and stylesheet.

Direction: the owner's Stitch design for Chidimma OS (obsidian rail, slate
canvas, white cards, a serif for display and a sans for the interface). The
tokens below are that design's, with four changes for legibility, each
measured rather than judged by eye:

  muted text   #94A3B8 -> #64748B   2.56:1 on white fails 4.5:1; 4.76:1 passes
  approved     #059669 -> #047857   white on it was 3.77:1; now 5.48:1
  attention    #D97706 -> #92400E   on its amber tint 3.07:1; now 6.84:1
  field edges  #CBD5E1 -> #64748B   1.48:1 against white fails 3:1; 4.76:1 passes

The design's two faces, Playfair Display and Inter, ship with the app in
static/fonts (SIL Open Font License; the licence files sit beside them). The
pages load them from this server and call no font host. The latin-ext files
carry the marked letters of Yoruba and Igbo names and the naira sign.
"""

from __future__ import annotations

from html import escape

CSS = """
@font-face { font-family: "Inter"; font-style: normal; font-weight: 100 900; font-display: swap;
  src: url("/static/fonts/inter-latin-wght-normal.woff2") format("woff2");
  unicode-range: U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD; }
@font-face { font-family: "Inter"; font-style: normal; font-weight: 100 900; font-display: swap;
  src: url("/static/fonts/inter-latin-ext-wght-normal.woff2") format("woff2");
  unicode-range: U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,U+0329,U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF; }
@font-face { font-family: "Playfair Display"; font-style: normal; font-weight: 400 900; font-display: swap;
  src: url("/static/fonts/playfair-display-latin-wght-normal.woff2") format("woff2");
  unicode-range: U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD; }
@font-face { font-family: "Playfair Display"; font-style: normal; font-weight: 400 900; font-display: swap;
  src: url("/static/fonts/playfair-display-latin-ext-wght-normal.woff2") format("woff2");
  unicode-range: U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,U+0329,U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF; }
:root {
  --rail: #0B0E14; --rail-text: #94A3B8; --rail-active: #1E293B; --inverse: #F8FAFC;
  --canvas: #F8FAFC; --card: #FFFFFF; --subtle: #F1F5F9; --line: #E2E8F0; --edge: #64748B;
  --ink: #0F172A; --text: #475569; --muted: #64748B;
  --ok: #047857; --ok-ink: #065F46; --ok-tint: #ECFDF5;
  --warn-ink: #92400E; --warn-tint: #FFFBEB; --warn: #B45309;
  --bad: #BE123C; --bad-ink: #9F1239; --bad-tint: #FFF1F2;
  --link: #4338CA; --focus: #4F46E5;
  --serif: "Playfair Display", Georgia, "Times New Roman", serif;
  --sans: Inter, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--canvas); color: var(--text); font: 14px/1.5 var(--sans); }
a { color: var(--link); }
:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; border-radius: 4px; }

/* The rail: the design's one dark surface. It holds the name and the five screens. */
.rail { position: fixed; inset: 0 auto 0 0; width: 256px; background: var(--rail); color: var(--rail-text);
  display: flex; flex-direction: column; padding: 24px 12px 16px; }
.rail :focus-visible { outline-color: #A5B4FC; }
.brand { color: var(--inverse); font-weight: 600; letter-spacing: .04em; padding: 0 12px; }
.brand small { display: block; color: var(--rail-text); font-weight: 400; font-size: 12px; letter-spacing: .08em;
  text-transform: uppercase; margin-top: 4px; }
.rail nav { margin-top: 28px; display: flex; flex-direction: column; gap: 4px; }
.rail nav a, .rail button { display: flex; align-items: center; justify-content: space-between; min-height: 44px;
  padding: 0 12px; border-radius: 12px; color: var(--rail-text); text-decoration: none; font: inherit; font-size: 15px; }
.rail nav a:hover, .rail button:hover { color: var(--inverse); }
.rail nav a[aria-current="page"] { background: var(--rail-active); color: #FFFFFF; }
.count { background: var(--bad); color: #FFFFFF; border-radius: 999px; min-width: 22px; padding: 0 7px;
  font-size: 12px; font-weight: 600; line-height: 22px; text-align: center; }
.rail form { margin-top: auto; }
.rail button { width: 100%; background: none; border: 0; cursor: pointer; }

main { margin-left: 256px; padding: 40px 48px 96px; max-width: 1040px; }
h1 { font: 500 36px/1.2 var(--serif); letter-spacing: -.02em; color: var(--ink); margin: 0 0 8px; }
h2 { font: 600 18px/1.3 var(--sans); color: var(--ink); margin: 40px 0 12px; }
h3 { font: 600 15px/1.4 var(--sans); color: var(--ink); margin: 0 0 4px; }
.card h2 { font: 500 22px/1.25 var(--serif); letter-spacing: -.01em; margin: 0 0 4px; }
.lede { font-size: 15px; max-width: 68ch; margin: 0 0 24px; }
.meta { color: var(--muted); font-size: 12px; margin: 4px 0; }
.meta code, code { font-family: var(--mono); font-size: 12px; overflow-wrap: anywhere; }

.card { background: var(--card); border: 1px solid var(--line); border-radius: 16px; padding: 24px;
  margin: 16px 0; box-shadow: 0 1px 3px rgba(15, 23, 42, .05); }
.strip { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin: 0 0 8px; }
.strip a { display: block; background: var(--card); border: 1px solid var(--line); border-radius: 16px;
  padding: 16px 20px; text-decoration: none; color: var(--text); }
.strip strong { display: block; font: 600 28px/1.2 var(--sans); color: var(--ink); font-variant-numeric: tabular-nums; }

/* Drafted content is prose to read, so it is set as prose, at a readable measure. */
pre { white-space: pre-wrap; overflow-wrap: anywhere; background: var(--subtle); color: var(--ink);
  border-radius: 12px; padding: 16px; margin: 12px 0; font: 13px/1.6 var(--mono); }
pre.say { font: 15px/1.6 var(--sans); max-width: 72ch; }
.notes { margin: 12px 0; padding-left: 18px; }
.error { color: var(--bad-ink); font-weight: 600; }

/* One filled button per card: the decision. The rest are quiet. */
button, .button { min-height: 44px; padding: 0 16px; border-radius: 12px; border: 1px solid var(--edge);
  background: var(--card); color: var(--ink); font: 500 14px var(--sans); cursor: pointer; }
button:hover { background: var(--subtle); }
.approve { background: var(--ink); border-color: var(--ink); color: #FFFFFF; }
.approve:hover { background: #1E293B; }
.reject { border-color: var(--bad); color: var(--bad); }
.read { min-height: 44px; font-size: 13px; margin: 8px 0; }
input[type="text"], input[type="email"], input[type="number"], input[type="password"], input[type="datetime-local"], textarea { min-height: 44px; padding: 8px 12px;
  border: 1px solid var(--edge); border-radius: 12px; background: var(--card); color: var(--ink); font: 14px var(--sans); }
textarea { width: 100%; line-height: 1.5; }
label { color: var(--ink); }
form { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 8px 0; }
form.edit, form.stack { flex-direction: column; align-items: stretch; }
.actions { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin-top: 16px; }
.actions form { margin: 0; }
details { margin: 8px 0; }
summary { cursor: pointer; color: var(--ink); min-height: 44px; display: flex; align-items: center; }

/* Status is a word first; the tint is a second signal. */
[class^="s-"], [class*=" s-"] { display: inline-block; font: 600 12px/1.4 var(--sans); padding: 2px 10px;
  border-radius: 999px; background: var(--subtle); color: var(--text); vertical-align: middle; }
.s-done { background: var(--ok-tint); color: var(--ok-ink); }
.s-failed, .s-cancelled { background: var(--bad-tint); color: var(--bad-ink); }
.s-waiting, .s-waiting_for_approval, .s-waiting_for_input { background: var(--warn-tint); color: var(--warn-ink); }
.banner { border: 1px solid var(--warn); background: var(--warn-tint); color: var(--warn-ink); border-radius: 12px;
  padding: 12px 16px; margin: 16px 0; }
.banner.stop { border-color: var(--bad); background: var(--bad-tint); color: var(--bad-ink); }

.tablewrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; }
th { text-align: left; font-weight: 600; color: var(--ink); }
th, td { padding: 10px 12px 10px 0; border-bottom: 1px solid var(--line); vertical-align: top; }
td { font-variant-numeric: tabular-nums; }
.total { font: 600 28px/1.2 var(--sans); color: var(--ink); margin: 16px 0 4px; font-variant-numeric: tabular-nums; }
.total .meta { font-weight: 400; font-size: 14px; }
.month a { margin: 0 12px; }
.bar { background: var(--line); border-radius: 999px; height: 8px; margin-top: 8px; max-width: 420px; }
.fill { height: 100%; border-radius: 999px; background: var(--ok); }
.fill.alert { background: var(--warn); } .fill.stopped { background: var(--bad); }
.call { border-left: 2px solid var(--line); padding-left: 16px; margin: 12px 0; }

.signin { max-width: 400px; margin: 12vh auto; padding: 0 20px; }
.signin form { flex-direction: column; align-items: stretch; gap: 16px; margin-top: 24px; }
.signin label { display: flex; flex-direction: column; gap: 6px; font-weight: 500; }

/* Phone width: the rail becomes a bar across the top, and nothing scrolls sideways. */
@media (max-width: 860px) {
  .rail { position: static; width: auto; flex-direction: row; flex-wrap: wrap; align-items: center; padding: 12px; gap: 4px 8px; }
  .brand small { display: none; }
  .rail nav { margin: 0; flex-direction: row; flex-wrap: wrap; }
  .rail form { margin: 0 0 0 auto; } .rail button { width: auto; }
  main { margin: 0; padding: 24px 16px 64px; }
  h1 { font-size: 28px; }
  .strip { grid-template-columns: 1fr; }
  .card { padding: 16px; }
}
@media (prefers-reduced-motion: no-preference) {
  button, .rail nav a { transition: background-color .15s ease-out, color .15s ease-out; }
}
"""

SCREENS = [
    ("queue", "/queue", "Approval queue"),
    ("runs", "/inspect", "Runs"),
    ("calls", "/calendar", "Calls"),
    ("clients", "/files", "Clients"),
    ("library", "/library", "Skill library"),
    ("cost", "/cost", "Settings and cost"),
]


def page(title: str, active: str, body: str, *, brand: str, waiting: int = 0, signed_in: bool = False,
         script: str = "") -> str:
    """One screen inside the shell. `waiting` is the number of items that need
    the owner's decision; it shows beside the queue link on every screen."""
    links = []
    for key, href, label in SCREENS:
        current = ' aria-current="page"' if key == active else ""
        count = (
            f'<span class="count" aria-label="{waiting} waiting">{waiting}</span>' if key == "queue" and waiting else ""
        )
        links.append(f'<a href="{href}"{current}><span>{label}</span>{count}</a>')
    sign_out = '<form method="post" action="/logout"><button>Sign out</button></form>' if signed_in else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta name="robots" content="noindex, nofollow"/>
<title>{escape(title)} · {escape(brand)}</title>
<style>{CSS}</style></head>
<body>
<header class="rail">
  <div class="brand">{escape(brand)}<small>Private workspace</small></div>
  <nav aria-label="Screens">{"".join(links)}</nav>
  {sign_out}
</header>
<main>
{body}
</main>
{script}
</body></html>"""


def sign_in_page(brand: str, message: str = "", next_path: str = "/queue", with_code: bool = False) -> str:
    note = f'<p class="error" role="alert">{escape(message)}</p>' if message else ""
    code = (
        """<label>Six-digit code from your authenticator app <input type="text" name="code" inputmode="numeric"
      autocomplete="one-time-code" pattern="[0-9 ]{6,7}" required/></label>"""
        if with_code else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta name="robots" content="noindex, nofollow"/>
<title>Sign in · {escape(brand)}</title>
<style>{CSS}</style></head>
<body>
<main class="signin" style="margin-left:auto">
  <h1>{escape(brand)}</h1>
  <p class="lede">Sign in with your email and password.</p>
  {note}
  <form method="post" action="/login">
    <input type="hidden" name="next" value="{escape(next_path)}"/>
    <label>Email <input type="email" name="email" autocomplete="username" required autofocus/></label>
    <label>Password <input type="password" name="password" autocomplete="current-password" required/></label>
    {code}
    <button class="approve">Sign in</button>
  </form>
</main>
</body></html>"""


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"
