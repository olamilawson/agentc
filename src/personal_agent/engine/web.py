"""Reading the web for a research step: one search through the web_search tool (Exa).

The search's query is the owner's own question or request. The pages it returns
are outside material: each travels to the model as marked data, under its title
and link, and instruction-like text inside a page is flagged and never obeyed.

A search that cannot run (no key, no credit, a restricted workstream, Exa down)
does not stop the work: the step goes on without the web and says so.
"""

from __future__ import annotations

TOOL = "web_search"

# Added to a step's instruction when web pages are in its material.
CITE = (
    " Some material is marked as a web page, with its title and link: it is what a web search on the owner's "
    "question returned, and it was not checked. For each point you take from a web page, give its link. Say where "
    "pages disagree, and say when the pages do not settle the question. Never cite a page that is not in the "
    "material, and never present a page's claim as established fact."
)


def gather(h, state: dict, query: str, node: str) -> tuple[list[tuple[str, str]], list[dict], str | None]:
    """(material chunks, sources, a note when the search could not run)."""
    try:
        found = h.tools.call(state["run_id"], node, [TOOL], TOOL, {"client": state["client"], "query": query[:500]})
    except Exception as e:  # the step goes on without the web, and the owner is told why
        return [], [], f"The web was not searched: {str(e)[:300]}"
    results = found.get("results") or []
    if not results:
        return [], [], "The web search found no page."
    chunks = [(f"web page: {r['title']} ({r['url']})", r["text"]) for r in results]
    sources = [{"title": r["title"], "url": r["url"], "published": r.get("published")} for r in results]
    return chunks, sources, None
