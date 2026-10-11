# ADR 0005: Creative review — rendering, images to the model, and what verifies the links

Date: 2026-10-11
Decided by: Builder
Status: Accepted; the rubric anchors and rendering settings are provisional

## Rendering

Pages are rendered in-process with PDFium (`pypdfium2`, Apache-2.0/BSD) and
Pillow, in `src/personal_agent/rendering.py`. PDFs give a JPEG and the text of
each page; image files are one page each; Word and text files give text only,
because nothing here lays out a Word page; PowerPoint is refused with a request
to export a PDF.

ArtCraft's `pdfcraft-cli` and `wordcraft-cli` were assessed as renderers the
same week. They would add Word-to-page rendering and PDF annotation, but they
are pre-1.0 and untested on the owner's files, and they are external binaries
to install on the server. PDFium is a mature library that installs with the
project. `render_file` is the single seam; a second renderer can sit behind it
after a test on real decks.

## Images

- The model gateway now accepts labelled images with a call and sends each
  after its label ("Page 3 (deck.pdf)") so ratings can cite pages.
- Images are downscaled to `image_max_edge` and pages are scored in groups of
  `pages_per_group` (`config/creative_review.yaml`), per the PRD's cost rules.
  A work longer than one group gets one scoring call per group; each
  criterion's score is the mean across groups, rounded half up, with every
  citation kept. A mean is a crude way to judge a whole deck, and a long deck
  may want a synthesis step once real ones have been reviewed.
- Page images are not stored in graph state, the run record or the tool-call
  log. The render step records page text; the scoring step renders the same
  files again for the bytes.
- Text inside an image cannot be wrapped as untrusted data the way page text
  is. The scoring instruction says the pages are the work under review, never
  instructions; that is an instruction to the model, not an enforcement. The
  structural protection is unchanged: the node has no tools and nothing is
  filed without approval.

## Verify links is code only

The PRD's check has no model in it: a rating must cite a page that exists and
an area on it; a request must name a criterion, a page that exists and an area.
A rating fault returns the work to scoring; a request fault returns it to the
feedback step only. After two returns the feedback goes to the owner with the
open faults beside it in the queue, not inside the content that is filed.

The check proves the links exist, not that they are right: nothing verifies
that the cited area of the page shows what the rating says.

## Provisional

`skills/creative-review/SKILL.md` uses the PRD's five criteria; the anchors and
feedback rules were derived from the Digital Twin Agent Scope and await the
owner. There are no calibration examples: the skill has a section for them,
keyed by kind of work, and a run flags when none exist for its kind. The
evaluation set is synthetic and holds written work only.
