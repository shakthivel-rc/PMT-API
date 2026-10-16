"""Unit tests for multimodal document extractors and LLM adapter serialization."""
import io
import zipfile
from unittest.mock import MagicMock, patch

from ai_chat.extractors import (
    extract_docx_text,
    parse_document_for_llm,
    parse_tabular_csv_to_markdown,
)
from ai_chat.llm.anthropic_adapter import AnthropicAdapter
from ai_chat.llm.base import AttachmentPayload, LLMMessage
from ai_chat.llm.google_adapter import GoogleAdapter
from ai_chat.llm.openai_adapter import OpenAIAdapter


def test_parse_tabular_csv_to_markdown():
    csv_bytes = b"id,name,role\n1,Alice,Admin\n2,Bob,Developer\n"
    md = parse_tabular_csv_to_markdown(csv_bytes, max_rows=10)
    assert "| id | name | role |" in md
    assert "| 1 | Alice | Admin |" in md
    assert "| 2 | Bob | Developer |" in md


def test_parse_tabular_csv_truncation():
    rows = ["id,val"] + [f"{i},value_{i}" for i in range(15)]
    csv_bytes = "\n".join(rows).encode("utf-8")
    md = parse_tabular_csv_to_markdown(csv_bytes, max_rows=5)
    assert "| 4 | value_4 |" in md
    assert "additional rows omitted" in md


def test_extract_docx_text():
    # Build a tiny mock docx zip in-memory
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        xml_content = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            b'<w:body><w:p><w:r><w:t>Hello PMT PRD Document</w:t></w:r></w:p></w:body>'
            b'</w:document>'
        )
        z.writestr("word/document.xml", xml_content)

    text = extract_docx_text(buf.getvalue())
    assert text == "Hello PMT PRD Document"


def test_parse_document_image():
    png_bytes = b"\x89PNG\r\n\x1a\nfakeimagebytes"
    text, b64 = parse_document_for_llm("shot.png", png_bytes, "image/png")
    assert text is None
    assert b64 is not None


def test_parse_document_pdf():
    pdf_bytes = b"%PDF-1.4 sample content"
    with patch("pypdf.PdfReader") as mock_reader_cls:
        mock_page = MagicMock()
        mock_page.extract_text.return_value = "Page 1: Scope specification" * 10
        mock_reader = MagicMock()
        mock_reader.pages = [mock_page]
        mock_reader_cls.return_value = mock_reader

        text, b64 = parse_document_for_llm("spec.pdf", pdf_bytes, "application/pdf")
        assert "Scope specification" in text
        assert b64 is not None


def test_google_adapter_multimodal_image_formatting():
    adapter = GoogleAdapter(api_key="fake-key")
    attachment = AttachmentPayload(
        id="att-img-1",
        mime_type="image/png",
        data_base64="aW1hZ2VkYXRh",
        filename="screenshot.png",
    )
    msg = LLMMessage(
        role="user",
        content="Create a bug from this screenshot",
        attachments=[attachment],
    )

    _, contents = adapter._format_messages_and_system([msg])
    assert len(contents) == 1
    user_parts = contents[0]["parts"]
    assert len(user_parts) == 2
    assert "inlineData" in user_parts[0]
    assert user_parts[0]["inlineData"]["mimeType"] == "image/png"
    assert user_parts[0]["inlineData"]["data"] == "aW1hZ2VkYXRh"
    assert user_parts[1]["text"] == "Create a bug from this screenshot"


def test_google_adapter_document_text_formatting():
    adapter = GoogleAdapter(api_key="fake-key")
    attachment = AttachmentPayload(
        id="att-doc-1",
        mime_type="text/csv",
        text_content="| id | error |\n| 1 | 500 |",
        filename="errors.csv",
    )
    msg = LLMMessage(
        role="user",
        content="Process these defects",
        attachments=[attachment],
    )

    _, contents = adapter._format_messages_and_system([msg])
    user_parts = contents[0]["parts"]
    assert len(user_parts) == 2
    assert "Attached Document: errors.csv" in user_parts[0]["text"]
    assert user_parts[1]["text"] == "Process these defects"


def test_openai_adapter_multimodal_formatting():
    adapter = OpenAIAdapter(api_key="fake-key")
    attachment = AttachmentPayload(
        id="att-img-2",
        mime_type="image/jpeg",
        data_base64="anBlZ2RhdGE=",
        filename="ui.jpg",
    )
    msg = LLMMessage(
        role="user",
        content="Inspect UI",
        attachments=[attachment],
    )

    formatted = adapter._format_messages([msg])
    assert len(formatted) == 1
    content_list = formatted[0]["content"]
    assert isinstance(content_list, list)
    assert content_list[0]["type"] == "image_url"
    assert content_list[0]["image_url"]["url"] == "data:image/jpeg;base64,anBlZ2RhdGE="
    assert content_list[1]["text"] == "Inspect UI"


def test_anthropic_adapter_multimodal_formatting():
    adapter = AnthropicAdapter(api_key="fake-key")
    attachment = AttachmentPayload(
        id="att-img-3",
        mime_type="image/png",
        data_base64="cG5nZGF0YQ==",
        filename="wireframe.png",
    )
    msg = LLMMessage(
        role="user",
        content="Convert wireframe to tickets",
        attachments=[attachment],
    )

    _, formatted = adapter._format_messages_and_system([msg])
    assert len(formatted) == 1
    blocks = formatted[0]["content"]
    assert isinstance(blocks, list)
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"]["type"] == "base64"
    assert blocks[0]["source"]["data"] == "cG5nZGF0YQ=="
    assert blocks[1]["text"] == "Convert wireframe to tickets"


def test_google_adapter_multi_turn_normalization():
    adapter = GoogleAdapter(api_key="fake-key")
    from ai_chat.llm.base import LLMToolCall

    messages = [
        LLMMessage(role="system", content="System instruction"),
        LLMMessage(role="user", content="Turn 1 User"),
        LLMMessage(role="assistant", content="Turn 1 Assistant Part 1"),
        LLMMessage(role="assistant", content="Turn 1 Assistant Part 2"),  # Consecutive assistant
        LLMMessage(role="user", content="Turn 2 User"),
        LLMMessage(role="assistant", content="Turn 2 Assistant"),
    ]

    sys_inst, contents = adapter._format_messages_and_system(messages)
    assert sys_inst == {"parts": [{"text": "System instruction"}]}
    # Notice: trailing model turn MUST be popped to satisfy Gemini API "ends with user turn"
    assert contents[-1]["role"] == "user"
    # Consecutive assistant messages should be merged into single model turn
    assert contents[0]["role"] == "user"
    assert contents[1]["role"] == "model"
    assert len(contents[1]["parts"]) == 2
    assert contents[2]["role"] == "user"


def test_google_adapter_unfulfilled_tool_call_to_text():
    adapter = GoogleAdapter(api_key="fake-key")
    from ai_chat.llm.base import LLMToolCall

    # Tool call NOT followed by tool message (e.g. paused for confirmation or cancelled)
    messages = [
        LLMMessage(role="user", content="Create project SEC"),
        LLMMessage(
            role="assistant",
            content=None,
            tool_calls=[LLMToolCall(id="tc-1", name="space_create", arguments={"name": "SEC"})],
        ),
        LLMMessage(role="assistant", content="Action requires confirmation: space_create"),
        LLMMessage(role="user", content="Please inspect this log"),
    ]

    _, contents = adapter._format_messages_and_system(messages)
    assert len(contents) == 3
    assert contents[0]["role"] == "user"
    assert contents[1]["role"] == "model"
    # Function call was converted to text because no tool message answered it
    part_texts = [p.get("text", "") for p in contents[1]["parts"]]
    assert any("[Requested action: space_create" in t for t in part_texts)
    assert any("Action requires confirmation" in t for t in part_texts)
    assert contents[2]["role"] == "user"

