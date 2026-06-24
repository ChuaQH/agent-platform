"""Context resolver — preprocesses user message attachments before LLM call.

Extracts text from uploaded files (PDF, text, spreadsheets) and converts
images to multimodal content blocks for the LLM API.
"""

from __future__ import annotations

import base64
import csv
import io
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ResolvedContext:
    """Result of resolving a user message's attachments."""

    text_parts: list[str] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)


def resolve_attachments(elements: list[Any]) -> ResolvedContext | None:
    """Process uploaded file elements and extract text/images.

    Args:
        elements: List of Chainlit Element objects from message.elements.

    Returns:
        ResolvedContext with extracted text and image URLs, or None if no attachments.
    """
    if not elements:
        return None

    ctx = ResolvedContext()

    for el in elements:
        mime = getattr(el, "mime", "") or ""
        path = getattr(el, "path", None)
        url = getattr(el, "url", "")
        content = getattr(el, "content", None)
        name = getattr(el, "name", "")

        logger.info("Processing attachment: name=%s mime=%s", name, mime)

        if mime.startswith("image/"):
            image_url = _resolve_image(el, path, url, content)
            if image_url:
                ctx.image_urls.append(image_url)

        elif mime == "application/pdf":
            text_parts, image_urls = _extract_pdf_hybrid(path, name)
            if text_parts:
                ctx.text_parts.extend(text_parts)
            if image_urls:
                ctx.image_urls.extend(image_urls)

        elif mime in ("text/csv",):
            text = _extract_csv(path, name)
            if text:
                ctx.text_parts.append(text)

        elif _is_spreadsheet(mime, name):
            text = _extract_xlsx(path, name)
            if text:
                ctx.text_parts.append(text)

        elif mime.startswith("text/") or _is_text_extension(name):
            text = _extract_text_file(path, name)
            if text:
                ctx.text_parts.append(text)

        else:
            logger.info("Skipping unsupported attachment: %s (%s)", name, mime)

    if not ctx.text_parts and not ctx.image_urls:
        return None

    return ctx


def build_user_message(user_text: str, ctx: ResolvedContext) -> str:
    """Build the enriched user message with extracted file contents.

    Text content from files is prepended to the user's message.
    """
    parts: list[str] = []

    for text in ctx.text_parts:
        parts.append(text)

    if user_text.strip():
        parts.append(f"User message: {user_text.strip()}")

    return "\n\n".join(parts)


def build_multimodal_content(user_text: str, ctx: ResolvedContext) -> list[dict[str, Any]]:
    """Build a multimodal content array for the LLM API.

    Returns a list of content blocks suitable for OpenAI-compatible APIs
    that support vision (DeepSeek V4, GPT-4V, etc.).
    """
    content_blocks: list[dict[str, Any]] = []

    text_lines: list[str] = []
    for text in ctx.text_parts:
        text_lines.append(text)
    if user_text.strip():
        text_lines.append(user_text.strip())

    if text_lines:
        content_blocks.append({
            "type": "text",
            "text": "\n\n".join(text_lines),
        })

    for image_url in ctx.image_urls:
        content_blocks.append({
            "type": "image_url",
            "image_url": {"url": image_url, "detail": "auto"},
        })

    return content_blocks if content_blocks else [{"type": "text", "text": user_text}]


# ── Private helpers ─────────────────────────────────────────────────────


def _resolve_image(el: Any, path: str | None, url: str, content: Any) -> str | None:
    if url:
        return url
    if path and Path(path).exists():
        return _encode_image_file(Path(path))
    if isinstance(content, bytes):
        return _encode_image_bytes(content)
    return None


def _encode_image_file(filepath: Path) -> str | None:
    suffix = filepath.suffix.lower()
    mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp"}
    mime = mime_map.get(suffix, "image/png")
    try:
        data = filepath.read_bytes()
        b64 = base64.b64encode(data).decode()
        return f"data:{mime};base64,{b64}"
    except Exception as e:
        logger.error("Failed to encode image %s: %s", filepath, e)
        return None


def _encode_image_bytes(data: bytes) -> str:
    b64 = base64.b64encode(data).decode()
    return f"data:image/png;base64,{b64}"


def _extract_pdf_hybrid(path: str | None, name: str) -> tuple[list[str], list[str]]:
    """Extract text and images from a PDF.

    Text is extracted from every page. Pages with embedded images
    are also rendered as image URLs so the LLM can see visual content.
    """
    if not path or not Path(path).exists():
        return [], []

    try:
        import fitz
    except ImportError:
        logger.warning("pymupdf not installed, cannot extract PDF: %s", name)
        return [], []

    try:
        doc = fitz.open(str(path))
    except Exception as e:
        logger.error("Failed to open PDF %s: %s", name, e)
        return [], []

    text_parts: list[str] = []
    image_urls: list[str] = []
    page_texts: list[str] = []

    for page_num, page in enumerate(doc):
        page_text = page.get_text().strip()
        if page_text:
            page_texts.append(page_text)

        if page.get_images():
            img_url = _render_page_to_data_url(page, page_num)
            if img_url:
                image_urls.append(img_url)

    if page_texts:
        text_parts.append(f"[File: {name}]\n" + "\n\n".join(page_texts))

    doc.close()
    return text_parts, image_urls


def _render_page_to_data_url(page: Any, page_num: int) -> str | None:
    """Render a PDF page to a base64 PNG data URL."""
    try:
        import fitz
        mat = fitz.Matrix(2.0, 2.0)  # 2x zoom for readability
        pix = page.get_pixmap(matrix=mat)
        data = pix.tobytes("png")
        b64 = base64.b64encode(data).decode()
        return f"data:image/png;base64,{b64}"
    except Exception as e:
        logger.error("Failed to render PDF page %d: %s", page_num + 1, e)
        return None


def _extract_csv(path: str | None, name: str) -> str | None:
    if not path or not Path(path).exists():
        return None
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            return f"[File: {name}]\n(empty)"
        formatted = "\n".join("\t".join(row) for row in rows[:100])
        if len(rows) > 100:
            formatted += f"\n... ({len(rows) - 100} more rows)"
        return f"[File: {name}]\n{formatted}"
    except Exception as e:
        logger.error("Failed to extract CSV %s: %s", name, e)
    return None


def _extract_xlsx(path: str | None, name: str) -> str | None:
    if not path or not Path(path).exists():
        return None
    try:
        import openpyxl
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        parts: list[str] = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                continue
            parts.append(f"Sheet: {sheet_name}")
            for row in rows[:100]:
                parts.append("\t".join(str(c) if c is not None else "" for c in row))
            if len(rows) > 100:
                parts.append(f"... ({len(rows) - 100} more rows)")
        wb.close()
        return f"[File: {name}]\n" + "\n".join(parts)
    except ImportError:
        logger.warning("openpyxl not installed, cannot read spreadsheet: %s", name)
    except Exception as e:
        logger.error("Failed to read spreadsheet %s: %s", name, e)
    return None


def _extract_text_file(path: str | None, name: str) -> str | None:
    if not path or not Path(path).exists():
        return None
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        lines = text.split("\n")
        if len(lines) > 2000:
            text = "\n".join(lines[:2000])
            text += f"\n\n... truncated ({len(lines) - 2000} more lines)"
        return f"[File: {name}]\n{text}"
    except Exception as e:
        logger.error("Failed to read text file %s: %s", name, e)
    return None


def _is_spreadsheet(mime: str, name: str) -> bool:
    if mime in ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/vnd.ms-excel"):
        return True
    return Path(name).suffix.lower() in (".xlsx", ".xlsm", ".xls")


def _is_text_extension(name: str) -> bool:
    text_exts = {".txt", ".md", ".py", ".js", ".ts", ".html", ".css",
                 ".json", ".xml", ".yaml", ".yml", ".log", ".csv",
                 ".sh", ".bat", ".ps1", ".toml", ".ini", ".cfg", ".env"}
    return Path(name).suffix.lower() in text_exts
