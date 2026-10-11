"""Page rendering for creative review (PRD: creative review / render pages).

Converts creative work into page images and text without a model. PDFs render
page by page through PDFium; image files are one page each; Word and plain-text
files give text only, because nothing here lays out a Word page. Images are
downscaled to the configured longest edge: the lowest resolution that keeps the
rubric answerable is a cost control, not a quality setting.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

IMAGE_TYPES = (".png", ".jpg", ".jpeg", ".webp", ".gif")
TEXT_ONLY_TYPES = (".docx", ".txt", ".md")


class UnsupportedWork(ValueError):
    pass


@dataclass
class Page:
    source: str  # the file the page came from
    number: int  # numbered across all the files of one review, from 1
    text: str
    image: bytes | None  # JPEG, or None when the file type has no page image


def _jpeg(image, max_edge: int) -> bytes:
    image = image.convert("RGB")
    image.thumbnail((max_edge, max_edge))  # only ever shrinks; keeps the aspect ratio
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def render_file(data: bytes, filename: str, max_edge: int, first_number: int = 1) -> list[Page]:
    lower = filename.lower()
    if lower.endswith(".pdf"):
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(data)
        try:
            pages = []
            for index in range(len(pdf)):
                page = pdf[index]
                width, height = page.get_size()
                scale = max_edge / max(width, height)
                textpage = page.get_textpage()
                pages.append(
                    Page(
                        source=filename,
                        number=first_number + index,
                        text=textpage.get_text_bounded().replace("\r\n", "\n").strip(),
                        image=_jpeg(page.render(scale=scale).to_pil(), max_edge),
                    )
                )
            return pages
        finally:
            pdf.close()
    if lower.endswith(IMAGE_TYPES):
        from PIL import Image

        return [Page(filename, first_number, "", _jpeg(Image.open(io.BytesIO(data)), max_edge))]
    if lower.endswith(TEXT_ONLY_TYPES):
        from personal_agent.clients import parse_document

        return [Page(filename, first_number, parse_document(data, filename)["text"].strip(), None)]
    hint = " Export the deck as a PDF." if lower.endswith((".pptx", ".ppt", ".key")) else ""
    raise UnsupportedWork(f"cannot render {filename}: supported types are PDF, images, Word and text.{hint}")
