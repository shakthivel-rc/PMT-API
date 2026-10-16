"""Document parsing and multimodal payload extraction pipeline for AI Chat."""
from __future__ import annotations

import base64
import csv
import io
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from ai_chat.llm.base import AttachmentPayload
from models.chat_session import ChatMessageAttachment
from models.workspace import Workspace
from services import chat_attachment_service


def parse_tabular_csv_to_markdown(content: bytes, max_rows: int = 100) -> str:
    """Parse CSV bytes into a clean GitHub Flavored Markdown table."""
    try:
        text = content.decode("utf-8", errors="replace")
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            return ""

        header = rows[0]
        data_rows = rows[1:max_rows + 1]

        # Build markdown table
        md_lines = [
            "| " + " | ".join(cell.replace("|", "\\|").strip() for cell in header) + " |",
            "| " + " | ".join("---" for _ in header) + " |",
        ]
        for row in data_rows:
            # Pad or truncate row to header length
            cells = [
                row[i].replace("|", "\\|").strip() if i < len(row) else ""
                for i in range(len(header))
            ]
            md_lines.append("| " + " | ".join(cells) + " |")

        omitted = len(rows) - 1 - len(data_rows)
        if omitted > 0:
            md_lines.append(f"\n*({omitted} additional rows omitted for brevity)*")

        return "\n".join(md_lines)
    except Exception as exc:
        return f"[Failed to parse CSV table: {exc}]"


def extract_docx_text(content: bytes) -> str:
    """Extract plain text from DOCX archive using standard library zipfile and XML."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            if "word/document.xml" not in z.namelist():
                return ""
            xml_bytes = z.read("word/document.xml")
            # Parse internal DOCX XML to extract user visible text nodes
            tree = ET.fromstring(xml_bytes)  # noqa: S314
            texts = [
                node.text for node in tree.iter()
                if node.tag.endswith("}t") and node.text
            ]
            return " ".join(texts)
    except Exception:
        return ""


def parse_document_for_llm(
    filename: str, content: bytes, mime_type: str
) -> tuple[str | None, str | None]:
    """Parse raw bytes and return (text_content, data_base64)."""
    ext = Path(filename).suffix.lower()

    # 1. Images: native base64 for vision models
    if mime_type.startswith("image/") or ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
        b64 = base64.b64encode(content).decode("ascii")
        return None, b64

    # 2. PDF Documents: extract text + retain base64 for native multimodal vision
    if mime_type == "application/pdf" or ext == ".pdf":
        extracted_text = ""
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(content))
            pages = [p.extract_text() for p in reader.pages[:50] if p.extract_text()]
            extracted_text = "\n\n".join(pages).strip()
        except Exception as pypdf_err:
            extracted_text = f"[PDF text extraction notice: {pypdf_err}]"

        b64 = base64.b64encode(content).decode("ascii")
        text_summary = extracted_text if len(extracted_text) >= 100 else (
            f"[PDF Document: {filename} ({len(content)} bytes) - Sparse text, use visual vision]"
        )
        return text_summary, b64

    # 3. CSV Tabular files
    if mime_type == "text/csv" or ext == ".csv":
        table_md = parse_tabular_csv_to_markdown(content)
        return table_md, None

    # 4. DOCX Documents
    if (
        mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        or ext == ".docx"
    ):
        docx_text = extract_docx_text(content)
        return docx_text or f"[DOCX Document: {filename}]", None

    # 5. Plain text, markdown, json, logs
    try:
        decoded = content.decode("utf-8", errors="replace")
        return decoded[:200000], None
    except Exception:
        return f"[Document: {filename} ({len(content)} bytes)]", None


def build_attachment_payload(
    workspace: Workspace,
    attachment: ChatMessageAttachment,
) -> AttachmentPayload:
    """Read attachment from storage and construct LLM AttachmentPayload."""
    content_bytes = chat_attachment_service.read_chat_attachment_bytes(workspace, attachment)
    text_content, data_b64 = parse_document_for_llm(
        attachment.original_filename,
        content_bytes,
        attachment.mime_type,
    )
    return AttachmentPayload(
        id=attachment.id,
        mime_type=attachment.mime_type,
        data_base64=data_b64,
        text_content=text_content or attachment.extracted_text,
        filename=attachment.original_filename,
    )
