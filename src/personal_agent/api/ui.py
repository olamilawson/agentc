"""The shared page shell and stylesheet.

Direction: the owner's Stitch screens for Chidimma OS, ported from their markup
(stitch_executive_ai_web_agent/chidimma_os_executive_intelligence_dashboard):
the navy rail with its workstreams, the quiet top bar, Newsreader for display
and Plus Jakarta Sans for the interface, white cards on a near-white canvas,
tinted icon circles, small tag chips, a purple accent.

Two departures, both for legibility and both measured:

  secondary text  slate-400 #94A3B8 -> slate-500 #64748B on light surfaces
                  (2.56:1 on white fails the 4.5:1 minimum; 4.76:1 passes)
  field edges     slate-200 -> slate-500, so a field's edge is visible (4.76:1)

The two typefaces ship with the app in static/fonts (SIL Open Font License; the
licence files sit beside them), so a page calls no font host. Icons are drawn
here as small inline SVGs in the outline style the screens use.
"""

from __future__ import annotations

from html import escape

_LATIN = ("U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,"
          "U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD")
_LATIN_EXT = ("U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,U+0329,U+1D00-1DBF,"
              "U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF")
FONT_FILES = [
    ("Newsreader", "newsreader"),
    ("Plus Jakarta Sans", "plus-jakarta-sans"),
]
_FACES = "\n".join(
    f'@font-face {{ font-family: "{family}"; font-style: normal; font-weight: 200 800; font-display: swap;\n'
    f'  src: url("/static/fonts/{stem}-{subset}-wght-normal.woff2") format("woff2"); unicode-range: {ranges}; }}'
    for family, stem in FONT_FILES
    for subset, ranges in (("latin", _LATIN), ("latin-ext", _LATIN_EXT))
)

CSS = _FACES + """
:root {
  --rail: #0d1527; --rail-hover: #162238; --rail-active: #1c2a44; --rail-text: #94a3b8; --rail-line: #1e293b;
  --canvas: #fbfbfc; --card: #ffffff; --subtle: #f1f5f9; --line: #f1f5f9; --line-strong: #e2e8f0; --edge: #64748b;
  --ink: #0f172a; --text: #334155; --muted: #64748b;
  --purple: #9333ea; --purple-ink: #7e22ce; --purple-tint: #faf5ff; --purple-line: #f3e8ff;
  --blue: #2563eb; --blue-tint: #eff6ff;
  --rose: #e11d48; --rose-ink: #be123c; --rose-tint: #fff1f2;
  --amber-ink: #92400e; --amber: #d97706; --amber-tint: #fffbeb;
  --emerald-ink: #047857; --emerald-tint: #ecfdf5;
  --indigo: #6366f1; --indigo-tint: #eef2ff;
  --serif: "Newsreader", Georgia, "Times New Roman", serif;
  --sans: "Plus Jakarta Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  --shadow: 0 1px 2px rgba(15, 23, 42, .05);
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--canvas); color: var(--text); font: 14px/1.5 var(--sans); -webkit-font-smoothing: antialiased; }
a { color: var(--blue); }
svg.i { width: 16px; height: 16px; flex: none; fill: none; stroke: currentColor; stroke-width: 2;
  stroke-linecap: round; stroke-linejoin: round; }
:focus-visible { outline: 2px solid var(--purple); outline-offset: 2px; border-radius: 6px; }
::selection { background: var(--purple-line); color: #581c87; }

/* --- the rail --- */
.rail { position: fixed; inset: 0 auto 0 0; width: 256px; background: var(--rail); color: var(--rail-text);
  display: flex; flex-direction: column; border-right: 1px solid var(--rail-line); }
.rail :focus-visible { outline-color: #c4b5fd; }
.brand { padding: 24px 24px 16px; color: #fff; font-size: 17px; font-weight: 600; letter-spacing: .05em; text-transform: uppercase; }
.brand small { display: block; margin-top: 2px; color: var(--rail-text); font-size: 10px; font-weight: 500; letter-spacing: .1em; }
.rail-scroll { flex: 1; overflow-y: auto; padding: 8px 12px 0; }
.rail nav { display: flex; flex-direction: column; gap: 4px; }
.rail a, .rail button { display: flex; align-items: center; gap: 12px; min-height: 38px; padding: 0 12px; border-radius: 8px;
  color: var(--rail-text); text-decoration: none; font: 500 14px var(--sans); background: none; border: 0; cursor: pointer;
  width: 100%; box-shadow: none; text-align: left; }
.rail a:hover, .rail button:hover { color: #fff; background: var(--rail-hover); }
.rail a[aria-current="page"] { background: var(--rail-active); color: #fff; }
.rail a .grow { flex: 1; }
.count { background: #e11d48; color: #fff; border-radius: 999px; min-width: 20px; height: 20px; padding: 0 6px;
  font-size: 11px; font-weight: 600; line-height: 20px; text-align: center; }
.count.quiet-count { background: var(--rail-active); color: #e2e8f0; }
.rail a[aria-current="page"] .quiet-count { background: #334155; }
.rail h2 { display: flex; align-items: center; justify-content: space-between; margin: 24px 0 8px; padding: 0 12px;
  color: var(--rail-text); font: 600 11px var(--sans); letter-spacing: .06em; text-transform: uppercase; }
.rail h2 a { min-height: 24px; width: 24px; padding: 0; justify-content: center; }
.streams a { min-height: 32px; font-size: 13px; font-weight: 400; color: #cbd5e1; }
.streams p { margin: 0; padding: 0 12px; font-size: 12px; }
.badge { width: 20px; height: 20px; border-radius: 4px; color: #fff; display: flex; align-items: center; justify-content: center;
  font-size: 10px; font-weight: 600; flex: none; }
.rail-foot { padding: 12px; border-top: 1px solid var(--rail-line); display: flex; flex-direction: column; gap: 2px; }
.rail-foot a, .rail-foot button { font-size: 12px; min-height: 34px; }
.rail-foot form { margin: 0; }

/* --- the page --- */
.page { margin-left: 256px; min-height: 100vh; display: flex; flex-direction: column; }
.topbar { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 16px 32px; color: var(--muted); font-size: 12px; }
.topbar .private { display: flex; align-items: center; gap: 8px; font-size: 11px; font-weight: 500; letter-spacing: .04em; text-transform: uppercase; }
.topbar .private svg.i { width: 12px; height: 12px; }
.topbar .right { display: flex; align-items: center; gap: 24px; text-align: right; }
.topbar .date { color: var(--text); font-weight: 500; }
.topbar .tagline { font-size: 11px; }
.bell { position: relative; color: var(--muted); display: flex; min-height: 32px; min-width: 32px; align-items: center; justify-content: center; }
.bell.on::after { content: ""; position: absolute; top: 6px; right: 6px; width: 6px; height: 6px; border-radius: 50%; background: #e11d48; }
main { padding: 8px 32px 64px; max-width: 1400px; width: 100%; }
main.has-dock { padding-bottom: 168px; }

h1 { font: 400 36px/1.15 var(--serif); letter-spacing: -.02em; color: var(--ink); margin: 0 0 4px; }
h2 { font: 500 20px/1.3 var(--serif); color: var(--ink); margin: 36px 0 12px; }
h3 { font: 600 14px/1.4 var(--sans); color: var(--ink); margin: 0 0 4px; }
.card h2 { margin: 0 0 4px; }
.lede { font-size: 14px; color: var(--muted); max-width: 70ch; margin: 0 0 24px; }
.meta { color: var(--muted); font-size: 12px; margin: 4px 0; }
code { font-family: var(--mono); font-size: 12px; overflow-wrap: anywhere; }

.card { background: var(--card); border: 1px solid var(--line-strong); border-radius: 16px; padding: 24px; margin: 16px 0; box-shadow: var(--shadow); }
.linkrow { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.linkrow h2 { margin: 0; }
.linkrow a { font-size: 12px; font-weight: 600; text-decoration: none; display: inline-flex; align-items: center; gap: 4px; min-height: 32px; }
.linkrow a svg.i { width: 12px; height: 12px; }
.eyebrow { color: var(--muted); font: 600 10px var(--sans); letter-spacing: .08em; text-transform: uppercase; margin: 0 0 8px; }
.quiet { color: var(--muted); font-size: 13px; margin: 12px 0 0; }

/* --- home --- */
.hero { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 20px; margin: 24px 0;
  border: 1px solid var(--purple-line); border-radius: 16px; box-shadow: var(--shadow); text-decoration: none; color: inherit;
  background: linear-gradient(90deg, #faf5ff, #fdfaff 55%, #ffffff); }
.hero .lead { display: flex; gap: 16px; align-items: flex-start; }
.hero .myview { color: var(--purple-ink); font-size: 12px; font-weight: 600; }
.hero strong { display: block; color: var(--ink); font-size: 16px; font-weight: 600; line-height: 1.35; margin: 2px 0; }
.hero small { color: var(--muted); font-size: 12px; }
.disc { width: 48px; height: 48px; border-radius: 50%; display: flex; align-items: center; justify-content: center; flex: none; }
.disc svg.i { width: 20px; height: 20px; }
.disc.purple { background: var(--purple-line); color: var(--purple); } .disc.rose { background: var(--rose-tint); color: var(--rose); }
.disc.amber { background: var(--amber-tint); color: var(--amber); } .disc.indigo { background: var(--indigo-tint); color: var(--indigo); }
.go { width: 36px; height: 36px; border-radius: 50%; border: 1px solid var(--line-strong); background: #fff; color: var(--muted);
  display: flex; align-items: center; justify-content: center; flex: none; box-shadow: var(--shadow); }
.metrics { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; }
.metric { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 20px; background: #fff;
  border: 1px solid var(--line-strong); border-radius: 16px; box-shadow: var(--shadow); text-decoration: none; color: inherit; }
.metric:hover, .hero:hover { box-shadow: 0 2px 8px rgba(15, 23, 42, .08); }
.metric .lead { display: flex; align-items: center; gap: 16px; }
.metric b { display: block; font-size: 24px; font-weight: 700; line-height: 1; color: var(--ink); font-variant-numeric: tabular-nums; }
.metric b + span { display: block; font-size: 14px; font-weight: 600; color: #1e293b; margin-top: 4px; }
.metric small { color: var(--muted); font-size: 12px; }
.metric > svg.i { color: var(--muted); }
.columns { display: grid; grid-template-columns: 5fr 3.5fr 3.5fr; gap: 24px; align-items: start; margin-top: 24px; }
.columns.two { grid-template-columns: 1fr 1fr; }
.columns .card { margin: 0; }
.chips + h3 { margin-top: 10px; }
form.route { display: grid; grid-template-columns: 1.2fr 1fr 1fr 1fr; gap: 12px 16px; align-items: end; padding: 16px 0; margin: 0; }
form.route + form.route { border-top: 1px solid var(--line-strong); }
form.route label { display: flex; flex-direction: column; gap: 6px; font-size: 12px; font-weight: 600; min-width: 0; }
form.route select, form.route input { width: 100%; }
form.route .actions { grid-column: 1 / -1; margin: 0; }
.rows { margin: 0; padding: 0; list-style: none; }
.rows li + li { border-top: 1px solid var(--line); }
.rows a, .rows .row { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px 12px; margin: 0 -12px;
  border-radius: 12px; text-decoration: none; color: inherit; }
.rows a:hover { background: #f8fafc; }
.rows .lead { display: flex; align-items: flex-start; gap: 12px; min-width: 0; }
.rows b { display: block; font-size: 13px; font-weight: 600; color: var(--ink); line-height: 1.35; overflow-wrap: anywhere; }
.rows small { color: var(--muted); font-size: 11px; }
.rows .end { display: flex; align-items: center; gap: 8px; flex: none; color: var(--muted); }
.num { width: 28px; height: 28px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 600; flex: none; }
.tag { font-size: 10px; font-weight: 600; padding: 2px 8px; border-radius: 999px; border: 1px solid transparent; white-space: nowrap; }
.c-rose { background: var(--rose-tint); color: var(--rose-ink); border-color: #ffe4e6; }
.c-purple { background: var(--purple-tint); color: var(--purple-ink); border-color: var(--purple-line); }
.c-blue { background: var(--blue-tint); color: #1d4ed8; border-color: #dbeafe; }
.c-emerald { background: var(--emerald-tint); color: var(--emerald-ink); border-color: #d1fae5; }
.c-amber { background: var(--amber-tint); color: var(--amber-ink); border-color: #fef3c7; }
.c-slate { background: var(--subtle); color: #475569; }
.dot { width: 8px; height: 8px; border-radius: 50%; margin-top: 5px; flex: none; background: #64748b; }
.dot.done { background: #059669; } .dot.failed, .dot.cancelled { background: #e11d48; }
.dot.waiting_for_approval, .dot.waiting_for_input { background: #d97706; } .dot.running, .dot.queued { background: #2563eb; }

/* The dock: the one place to start work from the home screen. */
.dock { position: fixed; left: 256px; right: 0; bottom: 0; padding: 24px 32px 16px; pointer-events: none;
  background: linear-gradient(to top, var(--canvas) 75%, rgba(251, 251, 252, 0)); }
.dock > * { pointer-events: auto; max-width: 960px; margin-left: auto; margin-right: auto; }
.dock form { display: flex; flex-wrap: nowrap; align-items: center; gap: 8px; margin-top: 0; margin-bottom: 10px; padding: 6px 6px 6px 20px;
  background: #fff; border: 1px solid #d8b4fe; border-radius: 999px; box-shadow: 0 8px 24px rgba(88, 28, 135, .08); }
.dock input[type="text"] { flex: 1; min-width: 0; border: 0; min-height: 40px; padding: 0; background: none; }
.dock input[type="text"]:focus-visible { outline: none; }
.dock form:focus-within { outline: 2px solid var(--purple); outline-offset: 2px; }
.kbd { font: 500 11px var(--mono); color: var(--muted); background: var(--subtle); border: 1px solid var(--line-strong); border-radius: 6px; padding: 2px 6px; }
.dock button { width: 44px; min-height: 44px; padding: 0; border-radius: 50%; border: 0; background: var(--purple); color: #fff;
  display: flex; align-items: center; justify-content: center; flex: none; }
.dock button:hover { background: var(--purple-ink); }
.pills { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 12px; color: var(--muted); }
.pills a { display: inline-flex; align-items: center; gap: 6px; min-height: 32px; padding: 0 12px; border-radius: 999px; color: var(--text);
  text-decoration: none; font-weight: 500; }
.pills a:hover { background: var(--purple-tint); color: var(--purple-ink); }
.pills a svg.i { width: 14px; height: 14px; }
.pills .state { margin-left: auto; }

/* --- the review sheet: one decision, read in full, in a dialog --- */
dialog.sheet { width: min(768px, calc(100vw - 32px)); max-height: 90vh; padding: 0; border: 1px solid var(--line-strong);
  border-radius: 24px; background: #fff; color: var(--text); overflow: hidden; flex-direction: column;
  box-shadow: 0 25px 60px -15px rgba(15, 23, 42, .25); }
dialog.sheet[open] { display: flex; }
dialog.sheet::backdrop { background: rgba(15, 23, 42, .4); backdrop-filter: blur(8px); }
dialog.sheet:not(:modal) { position: static; width: auto; max-width: 768px; max-height: none; margin: 16px 0; }
.sheet-strip { height: 6px; flex: none; background: linear-gradient(90deg, #9333ea, #3b82f6, #10b981); }
.sheet-head { display: flex; align-items: flex-start; gap: 16px; padding: 20px 28px; border-bottom: 1px solid var(--line-strong); flex: none; }
.sheet-head .grow { flex: 1; min-width: 0; }
.sheet-head .badge { width: 44px; height: 44px; border-radius: 12px; font-size: 14px; }
.sheet-head h2 { font: 400 24px/1.25 var(--serif); letter-spacing: -.01em; margin: 6px 0 0; overflow-wrap: anywhere; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 0; }
.chips .tag:first-child { text-transform: uppercase; letter-spacing: .04em; }
.close { width: 40px; min-height: 40px; padding: 0; border-radius: 50%; border: 0; background: var(--subtle); color: #475569;
  display: flex; align-items: center; justify-content: center; flex: none; box-shadow: none; }
.close:hover { background: var(--line-strong); }
.sheet-body { padding: 20px 28px; overflow-y: auto; flex: 1; }
.sheet-body > * + * { margin-top: 16px; }
.sheet-body .eyebrow { margin: 0; font-size: 11px; }
.sheet-body .linkrow { margin-bottom: -8px; }
.sheet-body .read { margin: 0; min-height: 36px; font-size: 12px; }
.sheet-body pre { margin-bottom: 0; }
.context { display: flex; gap: 12px; align-items: flex-start; padding: 14px 16px; border-radius: 16px; background: #f8fafc;
  border: 1px solid var(--line-strong); font-size: 13px; line-height: 1.6; }
.context svg.i { width: 28px; height: 28px; padding: 6px; border-radius: 8px; background: #fff; border: 1px solid var(--line-strong); color: #475569; }
.context strong { color: var(--ink); }
.evaluation { display: flex; gap: 14px; align-items: flex-start; padding: 16px; border-radius: 16px; border: 1px solid var(--purple-line);
  background: linear-gradient(90deg, #faf5ff, #f5f7ff); }
.evaluation .mark { width: 36px; height: 36px; border-radius: 10px; background: var(--purple); color: #fff; display: flex;
  align-items: center; justify-content: center; flex: none; }
.evaluation .eyebrow { color: var(--purple-ink); }
.evaluation .notes { margin: 6px 0 0; font-size: 13px; color: var(--ink); }
.sheet-foot { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; padding: 16px 28px;
  background: #f8fafc; border-top: 1px solid var(--line-strong); flex: none; }
.sheet-foot .actions { margin: 0; }
.sheet-foot button { display: inline-flex; align-items: center; gap: 8px; }

/* --- content of the other screens --- */
.strip { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin: 0 0 8px; }
.strip a { display: block; background: var(--card); border: 1px solid var(--line-strong); border-radius: 16px; padding: 16px 20px;
  text-decoration: none; color: var(--text); box-shadow: var(--shadow); }
.strip strong { display: block; font: 700 24px/1.2 var(--sans); color: var(--ink); font-variant-numeric: tabular-nums; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; background: #f8fafc; color: var(--ink); border: 1px solid var(--line-strong);
  border-radius: 12px; padding: 16px; margin: 12px 0; font: 13px/1.6 var(--mono); }
pre.say { font: 15px/1.65 var(--sans); max-width: 72ch; }
.notes { margin: 12px 0; padding-left: 18px; }
.error { color: var(--rose-ink); font-weight: 600; }

button, .button { min-height: 44px; padding: 0 16px; border-radius: 12px; border: 1px solid var(--edge);
  background: var(--card); color: var(--ink); font: 600 14px var(--sans); cursor: pointer; box-shadow: var(--shadow); }
button:hover { background: #f8fafc; }
.approve { background: var(--ink); border-color: var(--ink); color: #fff; }
.approve:hover { background: #1e293b; }
.reject { border-color: var(--rose-ink); color: var(--rose-ink); }
.read { min-height: 44px; font-size: 13px; margin: 8px 0; }
select, input[type="text"], input[type="email"], input[type="number"], input[type="password"], input[type="datetime-local"], textarea {
  min-height: 44px; padding: 8px 12px; border: 1px solid var(--edge); border-radius: 12px; background: var(--card);
  color: var(--ink); font: 14px var(--sans); }
textarea { width: 100%; line-height: 1.5; }
input[type="file"] { min-height: 44px; padding: 10px 0; font: 14px var(--sans); max-width: 100%; }
label { color: var(--ink); }
form { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 8px 0; }
form.edit, form.stack { flex-direction: column; align-items: stretch; gap: 16px; }
form.stack label { display: flex; flex-direction: column; gap: 6px; font-weight: 600; font-size: 13px; }
form.stack button { align-self: flex-start; }
.or { color: var(--muted); font-size: 12px; font-weight: 400; }
.actions { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin-top: 16px; }
.actions form { margin: 0; }
details { margin: 8px 0; }
summary { cursor: pointer; color: var(--ink); min-height: 44px; display: flex; align-items: center; font-weight: 600; }

[class^="s-"], [class*=" s-"] { display: inline-block; font: 600 11px/1.5 var(--sans); padding: 2px 10px; border-radius: 999px;
  background: var(--subtle); color: #475569; vertical-align: middle; }
.s-done { background: var(--emerald-tint); color: var(--emerald-ink); }
.s-failed, .s-cancelled { background: var(--rose-tint); color: var(--rose-ink); }
.s-waiting, .s-waiting_for_approval, .s-waiting_for_input { background: var(--amber-tint); color: var(--amber-ink); }
.banner { border: 1px solid var(--amber); background: var(--amber-tint); color: var(--amber-ink); border-radius: 12px; padding: 12px 16px; margin: 16px 0; }
.banner.stop { border-color: var(--rose-ink); background: var(--rose-tint); color: var(--rose-ink); }

.tablewrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; }
th { text-align: left; font-weight: 600; color: var(--ink); font-size: 12px; }
th, td { padding: 10px 12px 10px 0; border-bottom: 1px solid var(--line-strong); vertical-align: top; }
td { font-variant-numeric: tabular-nums; }
.total { font: 700 28px/1.2 var(--sans); color: var(--ink); margin: 16px 0 4px; font-variant-numeric: tabular-nums; }
.total .meta { font-weight: 400; font-size: 14px; }
.month a { margin: 0 12px; }
.bar { background: var(--line-strong); border-radius: 999px; height: 8px; margin-top: 8px; max-width: 420px; }
.fill { height: 100%; border-radius: 999px; background: #059669; }
.fill.alert { background: var(--amber); } .fill.stopped { background: var(--rose); }
.call { border-left: 2px solid var(--line-strong); padding-left: 16px; margin: 12px 0; }

.signin { max-width: 400px; margin: 12vh auto; padding: 0 20px; }
.signin h1 { text-transform: uppercase; font: 600 20px var(--sans); letter-spacing: .05em; }
.signin form { flex-direction: column; align-items: stretch; gap: 16px; margin-top: 24px; }
.signin label { display: flex; flex-direction: column; gap: 6px; font-weight: 600; font-size: 13px; }

/* Phone width: the rail becomes a bar across the top, and nothing scrolls sideways. */
@media (max-width: 1100px) { .columns, .columns.two, form.route { grid-template-columns: 1fr; } }
@media (max-width: 860px) {
  .rail { position: static; width: auto; border-right: 0; }
  .brand { padding: 16px 16px 8px; } .brand small { display: none; }
  .rail-scroll { padding: 0 8px 8px; overflow: visible; }
  .rail nav { flex-direction: row; flex-wrap: wrap; }
  .rail a, .rail button { width: auto; min-height: 44px; }
  .rail h2, .streams { display: none; }
  .rail-foot { flex-direction: row; flex-wrap: wrap; padding: 4px 8px 8px; }
  .page { margin: 0; }
  .topbar { padding: 12px 16px; } .topbar .tagline, .topbar .private span { display: none; }
  .bell { min-height: 44px; min-width: 44px; }
  main { padding: 8px 16px 48px; } main.has-dock { padding-bottom: 24px; }
  h1 { font-size: 28px; }
  .metrics, .strip { grid-template-columns: 1fr; }
  .card { padding: 16px; }
  .dock { position: static; padding: 0 16px 24px; background: none; }
  .pills a, .linkrow a { min-height: 44px; } .pills .state { margin-left: 0; width: 100%; }
  .kbd { display: none; }
  .sheet-head, .sheet-body, .sheet-foot { padding-left: 16px; padding-right: 16px; }
  .close { width: 44px; min-height: 44px; } .sheet-body .read { min-height: 44px; }
  .sheet-foot, .sheet-foot .actions { flex-direction: column; align-items: stretch; } .sheet-foot button { justify-content: center; }
}
@media (prefers-reduced-motion: no-preference) {
  button, .rail a, .metric, .hero, .rows a { transition: background-color .15s ease-out, color .15s ease-out, box-shadow .15s ease-out; }
}
"""

# Outline icons, 24-unit grid, drawn for this app in the style of the screens' icon set.
_ICONS = {
    "home": '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/><path d="M10 21v-6h4v6"/>',
    "review": '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="m8 12 3 3 5-6"/>',
    "work": '<rect x="3" y="7" width="18" height="13" rx="2"/><path d="M9 7V5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/><path d="M3 13h18"/>',
    "runs": '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    "calls": '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "memory": '<path d="M12 6v14"/><path d="M12 6C10 4.5 7 4 4 4v14c3 0 6 .5 8 2 2-1.5 5-2 8-2V4c-3 0-6 .5-8 2z"/>',
    "folder": '<path d="M3 6a2 2 0 0 1 2-2h4l2 3h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "settings": '<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
    "out": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5M21 12H9"/>',
    "chevron": '<path d="m9 6 6 6-6 6"/>',
    "arrow": '<path d="M5 12h14M13 6l6 6-6 6"/>',
    "up": '<path d="M12 19V5M5 12l7-7 7 7"/>',
    "lock": '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
    "bell": '<path d="M6 16V11a6 6 0 0 1 12 0v5l2 2H4z"/><path d="M10 21h4"/>',
    "spark": '<path d="M11 4l1.7 4.8L17.5 10.5l-4.8 1.7L11 17l-1.7-4.8L4.5 10.5l4.8-1.7z"/><path d="M19 3v4M17 5h4M19 17v3M17.5 18.5h3"/>',
    "decide": '<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4h6v3H9zM9 14l2 2 4-4"/>',
    "risk": '<path d="M12 4 2.5 20h19z"/><path d="M12 10v4M12 17h.01"/>',
    "brief": '<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5M10 13h6M10 17h4"/>',
    "creative": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m3 16 5-5 4 4 3-3 6 6"/><circle cx="9" cy="9" r="1.5"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
    "x": '<path d="M6 6l12 12M18 6 6 18"/>',
    "check": '<path d="m5 12 5 5 9-10"/>',
    "note": '<path d="M4 5h16v11H9l-5 4z"/><path d="M12 8v5M9.5 10.5h5"/>',
    "today": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    "think": '<path d="M9 18h6M10 21h4"/><path d="M12 3a6 6 0 0 0-4 10.5c.8.8 1.2 1.6 1.4 2.5h5.2c.2-.9.6-1.7 1.4-2.5A6 6 0 0 0 12 3z"/>',
    "intelligence": '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    "draft": '<path d="M4 20h4l11-11-4-4L4 16z"/><path d="m13 7 4 4"/>',
}


def icon(name: str) -> str:
    return f'<svg class="i" viewBox="0 0 24 24" aria-hidden="true">{_ICONS[name]}</svg>'


# White initials on each: the lightest, #0ea5e9 and #65a30d, were under 3:1, so they sit one step darker.
_STREAM_COLOURS = ("#e11d48", "#0284c7", "#059669", "#b45309", "#475569", "#7c3aed", "#0369a1", "#c2410c", "#4d7c0f")


def stream_badge(name: str) -> str:
    """A client's square, as the screens draw a workstream: its initials on a colour of its own."""
    words = [w for w in name.replace("-", " ").replace("_", " ").split() if w]
    initials = (words[0][0] + words[1][0]) if len(words) > 1 else name[:1]
    colour = _STREAM_COLOURS[sum(name.encode()) % len(_STREAM_COLOURS)]
    return f'<span class="badge" style="background:{colour}" aria-hidden="true">{escape(initials.upper())}</span>'


SCREENS = [
    ("home", "/", "Home", "home"),
    ("today", "/today", "Today", "today"),
    ("think", "/think", "Think", "think"),
    ("new", "/new", "New work", "work"),
    ("queue", "/queue", "Review", "review"),
    ("intelligence", "/intelligence", "Intelligence", "intelligence"),
]
FOOT = [
    ("library", "/library", "Memory &amp; decisions", "memory"),
    ("runs", "/inspect", "Runs", "runs"),
    ("calls", "/calendar", "Calls", "calls"),
    ("clients", "/files", "Clients", "folder"),
    ("cost", "/cost", "Settings and cost", "settings"),
]


def page(title: str, active: str, body: str, *, brand: str, waiting: int = 0, signed_in: bool = False,
         script: str = "", refresh: int = 0, clients: list[str] | None = None, today: str = "",
         dock: str = "", today_count: int = 0) -> str:
    """One screen inside the shell. `waiting` is the number of items that need the
    owner's decision; `clients` are listed in the rail as workstreams; `dock` is
    the bar fixed to the bottom of the home screen."""
    def link(key: str, href: str, label: str, glyph: str) -> str:
        current = ' aria-current="page"' if key == active else ""
        count = f'<span class="count" aria-label="{waiting} waiting">{waiting}</span>' if key == "queue" and waiting else ""
        if key == "today" and today_count:
            count = f'<span class="count quiet-count" aria-label="{today_count} today">{today_count}</span>'
        return f'<a href="{href}"{current}>{icon(glyph)}<span class="grow">{label}</span>{count}</a>'

    streams = "".join(
        f'<a href="/files">{stream_badge(name)}<span>{escape(name)}</span></a>' for name in clients or []
    ) or "<p>No clients yet.</p>"
    sign_out = f'<form method="post" action="/logout"><button>{icon("out")}Sign out</button></form>' if signed_in else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta name="robots" content="noindex, nofollow"/>
{f'<meta http-equiv="refresh" content="{refresh}"/>' if refresh else ""}
<title>{escape(title)} · {escape(brand)}</title>
<style>{CSS}</style></head>
<body>
<header class="rail">
  <div class="brand">{escape(brand)}.<small>Executive intelligence</small></div>
  <div class="rail-scroll">
    <nav aria-label="Screens">{"".join(link(*screen) for screen in SCREENS)}</nav>
    <h2>Workstreams <a href="/new" aria-label="Start work for a client">{icon("plus")}</a></h2>
    <div class="streams">{streams}</div>
  </div>
  <div class="rail-foot">
    {"".join(link(*screen) for screen in FOOT)}
    {sign_out}
  </div>
</header>
<div class="page">
  <div class="topbar">
    <div class="private">{icon("lock")}<span>Your private executive space</span></div>
    <div class="right">
      <a class="bell{" on" if waiting else ""}" href="/queue" aria-label="{waiting} waiting for your decision">{icon("bell")}</a>
      <div><div class="date">{escape(today)}</div><div class="tagline">A little clarity. A lot of possibility.</div></div>
    </div>
  </div>
  <main{' class="has-dock"' if dock else ""}>
{body}
  </main>
  {dock}
</div>
{script}
</body></html>"""


def sign_in_page(brand: str, message: str = "", next_path: str = "/", with_code: bool = False) -> str:
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
<main class="signin">
  <h1>{escape(brand)}.</h1>
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
