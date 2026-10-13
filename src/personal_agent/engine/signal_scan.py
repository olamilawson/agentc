"""Signal scan: the outside-signals feed for one workstream.

    collect (code, each hour) -> sort (one model step, cheap tier, only when something is new)

The owner names what to watch for a workstream: a search term, read through
Google News' public RSS search, or the address of any RSS or Atom feed. Each
hour the scan fetches those, keeps the headlines it has not seen, and puts them
in "Incoming" on the Intelligence screen. A cheap model step then sorts each
new headline into reputation, regulation, opportunity or not relevant, with one
line of why. If no model answers, the headlines still arrive, unsorted.

What an item is: a headline, its publisher, its date and its link, as the feed
gave them. It is a lead and not a finding. Nothing reaches the watchlist until
the owner adds it, and the agent never states what an article says beyond the
feed's own summary.

Feed text is outside material: it travels to the model as marked data, and an
instruction inside a headline is flagged and never obeyed.

What leaves the server: a watch term goes to Google News as a search; a feed's
address is fetched. With a restricted workstream the sorting step uses approved
providers alone, like every other model step.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Literal, TypedDict
from urllib.parse import quote, urlsplit

import httpx
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from personal_agent.engine import steps
from personal_agent.engine.graph import ROLE
from personal_agent.harness.context import build_context
from personal_agent.harness.contract import StepContract, StepFailed

WORKFLOW = "signal-scan"
NEWS_SEARCH = "https://news.google.com/rss/search?q={term}&hl=en-NG&gl=NG&ceid=NG:en"
MAX_BYTES, PER_SOURCE, SORT_AT_ONCE = 2_000_000, 12, 24
KINDS = ("reputation", "regulation", "opportunity")
# Tests replace this with a stand-in for the network.
TRANSPORT: httpx.BaseTransport | None = None
ATOM = "{http://www.w3.org/2005/Atom}"
TAG = re.compile(r"<[^>]+>")


class FeedError(Exception):
    """A source could not be read. The message says why, in the owner's terms."""


# --- reading a feed ----------------------------------------------------------------------------
def source_url(source: dict) -> str:
    return NEWS_SEARCH.format(term=quote(source["value"])) if source["kind"] == "term" else source["value"]


def check_address(url: str) -> None:
    """A feed address must be public https. The agent does not fetch from this server's own network."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise FeedError("a feed's address must start with https://")
    if TRANSPORT is not None:
        return
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parts.hostname, 443)}
    except OSError:
        raise FeedError(f"{parts.hostname} could not be found")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise FeedError("that address is on a private network, so the agent will not fetch it")


def _clean(text: str | None, limit: int) -> str:
    return " ".join(unescape(TAG.sub(" ", text or "")).split())[:limit]


def _when(text: str | None) -> str | None:
    if not text:
        return None
    try:
        moment = parsedate_to_datetime(text) if "," in text else datetime.fromisoformat(text.replace("Z", "+00:00"))
        return moment.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return None


def parse_feed(body: bytes) -> list[dict]:
    """The items of an RSS or Atom document: title, link, publisher, when, summary."""
    head = body[:4000].lower()
    if b"<!doctype" in head or b"<!entity" in body.lower():
        raise FeedError("the feed declares its own entities, so it was not read")  # no entity expansion from outside
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise FeedError("the address did not return a feed")
    items = []
    for node in root.iter("item"):  # RSS
        source = node.find("source")
        title = _clean(node.findtext("title"), 300)
        publisher = _clean(source.text if source is not None else "", 120)
        if publisher and title.endswith(f" - {publisher}"):  # Google News repeats the publisher after the headline
            title = title[: -len(publisher) - 3]
        items.append({"title": title, "link": (node.findtext("link") or "").strip(), "publisher": publisher,
                      "published_at": _when(node.findtext("pubDate")), "summary": _clean(node.findtext("description"), 400)})
    for node in root.iter(f"{ATOM}entry"):  # Atom
        link = next((e.get("href") for e in node.findall(f"{ATOM}link") if e.get("rel") in (None, "alternate")), "")
        items.append({"title": _clean(node.findtext(f"{ATOM}title"), 300), "link": (link or "").strip(),
                      "publisher": _clean(root.findtext(f"{ATOM}title"), 120),
                      "published_at": _when(node.findtext(f"{ATOM}updated") or node.findtext(f"{ATOM}published")),
                      "summary": _clean(node.findtext(f"{ATOM}summary"), 400)})
    if not items and root.tag not in ("rss", f"{ATOM}feed") and root.find("channel") is None:
        raise FeedError("the address did not return a feed")
    return [i for i in items if i["title"] and urlsplit(i["link"]).scheme in ("http", "https")]


def fetch(source: dict) -> list[dict]:
    url = source_url(source)
    check_address(url)
    try:
        with httpx.Client(transport=TRANSPORT, timeout=20.0, follow_redirects=True, max_redirects=3,
                          headers={"User-Agent": "personal-agent signal scan"}) as http:
            response = http.get(url)
    except httpx.HTTPError as e:
        raise FeedError(f"the source could not be reached ({type(e).__name__})")
    if response.status_code != 200:
        raise FeedError(f"the source answered {response.status_code}")
    if len(response.content) > MAX_BYTES:
        raise FeedError("the feed is over 2 MB, so it was not read")
    return parse_feed(response.content)[:PER_SOURCE]


def fingerprint(workstream: str, item: dict) -> str:
    # The same story from the same publisher is one item, whichever source found it.
    key = f"{workstream}|{item['publisher'].lower()}|{item['title'].lower()}"
    return hashlib.sha256(key.encode()).hexdigest()


# --- the graph ---------------------------------------------------------------------------------
class SortIn(BaseModel):
    workstream: str
    watching: list[str]
    count: int


class Sorted(BaseModel):
    id: str
    kind: Literal["reputation", "regulation", "opportunity", "none"]
    why: str = Field(max_length=300)


class SortOut(BaseModel):
    items: list[Sorted]


class ScanState(TypedDict, total=False):
    run_id: str
    client: str
    inputs: dict
    usage: dict
    flags: list[str]
    fallback_used: bool
    report: dict
    error: str | None


def scan_contracts() -> dict[str, StepContract]:
    return {
        "sort_signals": StepContract(
            node="sort_signals", tier="cheap", allowed_tools=[], input_schema=SortIn, output_schema=SortOut,
            instruction=(
                "Each headline in the material is marked with its id. For the workstream named, sort each one: "
                "reputation (how the organisation or its field is spoken of), regulation (a rule, a regulator, a "
                "policy), opportunity (a market, a need, a partner), or none (not relevant to this workstream). "
                "Give one plain line of why, from the headline and its summary alone: never state what the full "
                "article says, and never present the headline as verified. Respond as a single JSON object with "
                "key items, a list of objects with keys id, kind and why. Include every id once."
            ),
        )
    }


def scan_outputs(state: dict) -> dict | None:
    return state.get("report")


def collect(db, workstream: str) -> dict:
    """Fetch a workstream's sources and keep the headlines not seen before. No model is involved."""
    report, new_ids = {"sources": 0, "new": 0, "failed": []}, []
    for source in db.list_watch_sources(workstream):
        report["sources"] += 1
        try:
            found = fetch(source)
        except FeedError as e:
            report["failed"].append({"source": source["value"], "why": str(e)})
            db.note_watch_source(source["id"], str(e))
            continue
        db.note_watch_source(source["id"], None)
        for item in found:
            item_id = db.add_feed_item(source["id"], workstream, fingerprint(workstream, item), **item)
            if item_id:
                new_ids.append(item_id)
    report["new"] = len(new_ids)
    return {**report, "new_ids": new_ids}


def build_scan_graph(h):
    db = h.db

    def sort_new(state: ScanState) -> dict:
        ids = list((state["inputs"] or {}).get("new_ids") or [])[:SORT_AT_ONCE]
        items = [i for i in (db.get_feed_item(item_id) for item_id in ids) if i and i["workstream"] == state["client"]]
        report = {"new": len(items), "sorted": 0}
        if not items:
            return {"report": report, "error": None}
        payload = {"workstream": state["client"], "count": len(items),
                   "watching": [s["value"] for s in db.list_watch_sources(state["client"]) if s["kind"] == "term"]}
        material = [(f"headline id {i['id']}", f"{i['title']}\nPublisher: {i['publisher']}\nSummary: {i['summary']}") for i in items]
        # Instruction-like text inside a headline is flagged on the run; the step treats it as data either way.
        _, flags = build_context(ROLE, "", [], material, {})
        out, usage = steps.run_model(h, "sort_signals", state, payload, "", material)
        known = {i["id"] for i in items}
        for verdict in out["items"]:
            if verdict["id"] in known:
                db.sort_feed_item(verdict["id"], verdict["kind"], verdict["why"])
                report["sorted"] += 1
        return {**usage, "report": report, "flags": state.get("flags", []) + flags, "error": None}

    builder = StateGraph(ScanState)
    builder.add_node("sort", sort_new)
    builder.add_edge(START, "sort")
    builder.add_edge("sort", END)
    return builder


def scan_all(engine, workstream: str | None = None) -> list[dict]:
    """Read every workstream's sources, then start one sorting run for each workstream that has
    something new. A workstream with nothing new starts no run, so an hourly scan leaves no trail
    of empty runs. If the sorting run fails (no model, or the monthly cap), the headlines still
    wait in Incoming, unsorted."""
    from personal_agent.costs import MonthlyCapReached

    done = []
    names = sorted({s["workstream"] for s in engine.db.list_watch_sources()})
    for ws in [workstream] if workstream else names:
        found = collect(engine.db, ws)
        result = {"workstream": ws, "sources": found["sources"], "new": found["new"], "failed": found["failed"]}
        if found["new_ids"]:
            try:
                run_id = engine.start_run(WORKFLOW, ws, {"new_ids": found["new_ids"]}, by_hand=False)
                run = engine.db.get_run(run_id)
                result["sorted"] = (run["outputs"] or {}).get("sorted", 0)
                if run["status"] == "failed":
                    result["not_sorted"] = run["error_reason"]
            except MonthlyCapReached as e:
                result["not_sorted"] = str(e)
        engine.db.audit(None, "signals_scanned", {k: result[k] for k in ("workstream", "sources", "new")})
        done.append(result)
    return done


def main(argv: list[str] | None = None) -> int:
    """`python -m personal_agent.engine.signal_scan`: scan every workstream's sources once."""
    from personal_agent.config import load_settings
    from personal_agent.db import Database
    from personal_agent.engine.runner import Engine

    settings = load_settings()
    results = scan_all(Engine(settings, Database(settings.database_url)))
    if not results:
        print("no source is set up")
    for r in results:
        failed = f", {len(r['failed'])} source(s) could not be read" if r["failed"] else ""
        print(f"{r['workstream']}: {r['new']} new from {r['sources']} source(s){failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
