"""Unit tests for chat attachment validation, text extraction, and storage ingestion."""
from unittest.mock import MagicMock, patch

import pytest

from models.chat_session import ChatMessageAttachment
from models.workspace import Workspace
from services.chat_attachment_service import (
    MAX_DOC_SIZE_BYTES,
    MAX_IMAGE_SIZE_BYTES,
    attachment_to_summary,
    extract_text_from_content,
    link_attachments,
    upload_chat_attachment,
    validate_and_normalize_mime,
)


def test_validate_and_normalize_mime_png():
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    mime = validate_and_normalize_mime("screenshot.png", png_bytes, "image/png")
    assert mime == "image/png"


def test_validate_and_normalize_mime_jpeg():
    jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF"
    mime = validate_and_normalize_mime("photo.jpg", jpeg_bytes, "image/jpeg")
    assert mime == "image/jpeg"


def test_validate_and_normalize_mime_pdf():
    pdf_bytes = b"%PDF-1.4\n1 0 obj\n"
    mime = validate_and_normalize_mime("spec.pdf", pdf_bytes, "application/pdf")
    assert mime == "application/pdf"


def test_validate_and_normalize_mime_csv_text():
    csv_bytes = b"title,priority,description\nBug 1,high,Broken button\n"
    mime = validate_and_normalize_mime("defects.csv", csv_bytes, "text/csv")
    assert mime == "text/csv"


def test_validate_and_normalize_mime_rejection_malicious():
    exe_bytes = b"MZ\x90\x00\x03\x00\x00\x00"
    with pytest.raises(ValueError, match="Unsupported file type"):
        validate_and_normalize_mime("malware.exe", exe_bytes, "application/x-dosexec")


def test_validate_and_normalize_mime_rejection_oversized_image():
    huge_bytes = b"\x89PNG\r\n\x1a\n" + b"0" * (MAX_IMAGE_SIZE_BYTES + 10)
    with pytest.raises(ValueError, match="Image exceeds maximum size"):
        validate_and_normalize_mime("large.png", huge_bytes, "image/png")


def test_validate_and_normalize_mime_rejection_oversized_doc():
    huge_bytes = b"%PDF-1.4\n" + b"0" * (MAX_DOC_SIZE_BYTES + 10)
    with pytest.raises(ValueError, match="Document exceeds maximum size"):
        validate_and_normalize_mime("large.pdf", huge_bytes, "application/pdf")


def test_extract_text_from_csv():
    csv_content = b"key,summary\nPAY-1,Card failed\nPAY-2,Refund error\n"
    text = extract_text_from_content("tasks.csv", csv_content, "text/csv")
    assert "PAY-1" in text
    assert "Refund error" in text


def test_extract_text_from_markdown():
    md_content = b"# Product Requirements\n\n## Scope\n- Mobile auth\n- Payment flow"
    text = extract_text_from_content("prd.md", md_content, "text/markdown")
    assert "Product Requirements" in text
    assert "Payment flow" in text


def test_extract_text_from_pdf():
    # Basic PDF text check (with mock or actual pypdf)
    pdf_bytes = b"%PDF-1.4 dummy content"
    with patch("pypdf.PdfReader") as mock_reader_cls:
        mock_page = MagicMock()
        mock_page.extract_text.return_value = "Extracted PDF requirement text"
        mock_reader = MagicMock()
        mock_reader.pages = [mock_page]
        mock_reader_cls.return_value = mock_reader

        text = extract_text_from_content("doc.pdf", pdf_bytes, "application/pdf")
        assert text == "Extracted PDF requirement text"


def test_upload_chat_attachment_success():
    db = MagicMock()
    ws = Workspace(id="ws-test-1", name="Test Space")
    content = b"\x89PNG\r\n\x1a\nfakeimage"

    with patch(
        "integrations.mcp.cloud_storage_server.default_mcp_storage_server.call_tool"
    ) as mock_mcp:
        mock_mcp.return_value = {
            "storage_path": "attachments/chat/test.png",
            "storage_provider": "local",
        }
        att = upload_chat_attachment(
            db=db,
            workspace=ws,
            user_id="user-123",
            filename="test.png",
            content=content,
            declared_mime="image/png",
            conversation_id="conv-1",
        )

        assert att.workspace_id == "ws-test-1"
        assert att.original_filename == "test.png"
        assert att.mime_type == "image/png"
        assert att.file_size_bytes == len(content)
        assert db.add.called
        assert db.commit.called


def test_link_attachments_and_summary():
    db = MagicMock()
    att1 = ChatMessageAttachment(
        id="att-1",
        workspace_id="ws-1",
        user_id="u-1",
        original_filename="screenshot.png",
        stored_path="path/1",
        mime_type="image/png",
        file_size_bytes=1024,
    )
    query_mock = MagicMock()
    query_mock.filter.return_value.all.return_value = [att1]
    db.query.return_value = query_mock

    linked = link_attachments(db, ["att-1"], "conv-1", "msg-1")
    assert len(linked) == 1
    assert linked[0].conversation_id == "conv-1"
    assert linked[0].message_id == "msg-1"
    assert db.commit.called

    summary = attachment_to_summary("ws-1", att1)
    assert summary.id == "att-1"
    assert summary.filename == "screenshot.png"
    assert summary.preview_url == "/api/workspaces/ws-1/chat/attachments/att-1"
