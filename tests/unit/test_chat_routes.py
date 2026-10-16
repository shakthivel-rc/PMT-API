"""Unit tests for AI chat HTTP routes and error sanitization in routes/chat.py."""
import uuid
import pytest
from unittest.mock import MagicMock

from models.chat_session import ChatConversation, ChatMessageRecord
from models.user import User
from models.workspace import Workspace, WorkspaceMember
from routes.chat import (
    sanitize_error_message,
    list_conversations,
    get_conversation_history,
    delete_conversation,
    list_available_tools,
)
from dependencies.workspace import WorkspaceAccess
from fastapi import HTTPException


# =========================================================================
# 1. Error Message Sanitization Tests
# =========================================================================

def test_sanitize_error_message_redacts_api_keys():
    # Redact Google query param key
    raw_google_url = "Failed request: https://generativelanguage.googleapis.com/v1beta/models?key=AIzaSyA123456789SecretKeyXYZ"
    sanitized = sanitize_error_message(raw_google_url)
    assert "AIzaSyA123456789SecretKeyXYZ" not in sanitized
    assert "key=[REDACTED]" in sanitized

    # Redact Bearer token
    raw_bearer = "Authorization error: Bearer sk-proj-superSecretTokenVal123456789"
    sanitized_bearer = sanitize_error_message(raw_bearer)
    assert "sk-proj-superSecretTokenVal123456789" not in sanitized_bearer
    assert "Bearer [REDACTED]" in sanitized_bearer

    # Redact OpenAI sk- token
    raw_sk = "Error calling OpenAI API with key sk-987654321abcdefg"
    sanitized_sk = sanitize_error_message(raw_sk)
    assert "sk-987654321abcdefg" not in sanitized_sk
    assert "[REDACTED_KEY]" in sanitized_sk

    # Redact direct AIza key
    raw_aiza = "Invalid key AIzaSyD9876543210zyxwvutsrq"
    sanitized_aiza = sanitize_error_message(raw_aiza)
    assert "AIzaSyD9876543210zyxwvutsrq" not in sanitized_aiza
    assert "[REDACTED_KEY]" in sanitized_aiza

    # Empty / none string handles gracefully
    assert sanitize_error_message("") == ""
    assert sanitize_error_message(None) == ""


# =========================================================================
# 2. Conversation List, History, and Deletion Tests
# =========================================================================

def test_list_conversations_tenant_isolation(db):
    u = User(id=str(uuid.uuid4()), username="chat_tester", email="chat_test@example.com", first_name="Chat", last_name="Tester", organization="Acme", department="Eng")
    db.add(u)
    db.flush()

    ws1 = Workspace(id=str(uuid.uuid4()), name="WS 1", slug=f"ws1-{uuid.uuid4().hex[:6]}", created_by=u.id)
    ws2 = Workspace(id=str(uuid.uuid4()), name="WS 2", slug=f"ws2-{uuid.uuid4().hex[:6]}", created_by=u.id)
    db.add_all([ws1, ws2])
    db.flush()

    conv1 = ChatConversation(id=str(uuid.uuid4()), workspace_id=ws1.id, user_id=u.id, title="Conv 1", deleted=0)
    conv2 = ChatConversation(id=str(uuid.uuid4()), workspace_id=ws1.id, user_id=u.id, title="Conv 2", deleted=0)
    conv_deleted = ChatConversation(id=str(uuid.uuid4()), workspace_id=ws1.id, user_id=u.id, title="Deleted Conv", deleted=1)
    conv_other_ws = ChatConversation(id=str(uuid.uuid4()), workspace_id=ws2.id, user_id=u.id, title="Other WS Conv", deleted=0)

    db.add_all([conv1, conv2, conv_deleted, conv_other_ws])
    db.commit()

    access = WorkspaceAccess(workspace_id=ws1.id, user_id=u.id, role="member")
    resp = list_conversations(access=access, db=db)
    
    assert resp.status_code == 200
    conv_ids = [c["id"] for c in resp.data]
    assert conv1.id in conv_ids
    assert conv2.id in conv_ids
    assert conv_deleted.id not in conv_ids
    assert conv_other_ws.id not in conv_ids


def test_get_conversation_history_accumulates_tool_calls(db):
    u = User(id=str(uuid.uuid4()), username="history_tester", email="hist@example.com", first_name="Hist", last_name="Tester", organization="Acme", department="Eng")
    db.add(u)
    db.flush()

    ws = Workspace(id=str(uuid.uuid4()), name="WS Hist", slug=f"wshist-{uuid.uuid4().hex[:6]}", created_by=u.id)
    db.add(ws)
    db.flush()

    conv = ChatConversation(id=str(uuid.uuid4()), workspace_id=ws.id, user_id=u.id, title="Tool Calling Session", deleted=0)
    db.add(conv)
    db.flush()

    # User message
    msg_user = ChatMessageRecord(
        id=str(uuid.uuid4()),
        conversation_id=conv.id,
        role="user",
        content="What is the active sprint?",
    )
    # Intermediate tool execution
    msg_tc = ChatMessageRecord(
        id=str(uuid.uuid4()),
        conversation_id=conv.id,
        role="assistant",
        content=None,
        tool_calls=[{"tool": "sprint_list", "args": {"space_id": "sp-1"}}],
    )
    # Assistant response
    msg_assistant = ChatMessageRecord(
        id=str(uuid.uuid4()),
        conversation_id=conv.id,
        role="assistant",
        content="Sprint 1 is currently active with 8 open issues.",
    )
    db.add_all([msg_user, msg_tc, msg_assistant])
    db.commit()

    access = WorkspaceAccess(workspace_id=ws.id, user_id=u.id, role="member")
    resp = get_conversation_history(conversation_id=conv.id, access=access, db=db)

    assert resp.status_code == 200
    messages = resp.data["messages"]
    assert len(messages) == 2  # user and assistant (tool call merged into assistant)
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "What is the active sprint?"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == "Sprint 1 is currently active with 8 open issues."
    assert messages[1]["tool_calls"] is not None
    assert messages[1]["tool_calls"][0]["tool"] == "sprint_list"


def test_delete_conversation_soft_deletes(db):
    u = User(id=str(uuid.uuid4()), username="deleter", email="del@example.com", first_name="Del", last_name="User", organization="Acme", department="Eng")
    db.add(u)
    db.flush()

    ws = Workspace(id=str(uuid.uuid4()), name="WS Del", slug=f"wsdel-{uuid.uuid4().hex[:6]}", created_by=u.id)
    db.add(ws)
    db.flush()

    conv = ChatConversation(id=str(uuid.uuid4()), workspace_id=ws.id, user_id=u.id, title="To Delete", deleted=0)
    db.add(conv)
    db.commit()

    access = WorkspaceAccess(workspace_id=ws.id, user_id=u.id, role="member")
    resp = delete_conversation(conversation_id=conv.id, access=access, db=db)
    assert resp.status_code == 200

    db.refresh(conv)
    assert conv.deleted == 1

    # Attempting to fetch deleted conversation should raise 404
    with pytest.raises(HTTPException) as exc_info:
        get_conversation_history(conversation_id=conv.id, access=access, db=db)
    assert exc_info.value.status_code == 404


def test_list_available_tools_returns_enabled_tools():
    access = WorkspaceAccess(workspace_id="ws-1", user_id="u-1", role="member")
    resp = list_available_tools(access=access)
    assert resp.status_code == 200
    assert len(resp.data) >= 120
    for t in resp.data:
        assert t["exposure"] == "enabled"
        assert "name" in t
        assert "tier" in t
        assert "category" in t


# =========================================================================
# 3. Chat Turn Execution Route Tests
# =========================================================================

def test_handle_chat_turn_user_not_found(db):
    from routes.chat import handle_chat_turn
    from schemas.chat import ChatRequest

    access = WorkspaceAccess(workspace_id="ws-fake", user_id="user-nonexistent", role="member")
    body = ChatRequest(message="Hello")

    with pytest.raises(HTTPException) as exc_info:
        handle_chat_turn(body=body, access=access, db=db)
    assert exc_info.value.status_code == 404


def test_handle_chat_turn_success_with_mock_gateway(db, monkeypatch):
    from routes.chat import handle_chat_turn
    from schemas.chat import ChatRequest
    from ai_chat.gateway import ChatTurnResult

    u = User(id=str(uuid.uuid4()), username="turn_user", email="turn@example.com", first_name="Turn", last_name="User", organization="Acme", department="Eng")
    ws = Workspace(id=str(uuid.uuid4()), name="Turn WS", slug=f"wsturn-{uuid.uuid4().hex[:6]}", created_by=u.id)
    db.add_all([u, ws])
    db.commit()

    # Mock gateway execute_turn
    fake_result = ChatTurnResult(
        conversation_id="conv-123",
        reply="I am your PMT AI Assistant.",
        tool_calls_executed=[{"tool": "space_list", "args": {}}],
    )
    from ai_chat.gateway import chat_gateway
    monkeypatch.setattr(chat_gateway, "execute_turn", lambda **kwargs: fake_result)

    access = WorkspaceAccess(workspace_id=ws.id, user_id=u.id, role="member")
    body = ChatRequest(message="What spaces are here?")
    resp = handle_chat_turn(body=body, access=access, db=db)

    assert resp.status_code == 200
    assert resp.data.conversation_id == "conv-123"
    assert resp.data.reply == "I am your PMT AI Assistant."
    assert len(resp.data.tool_calls_executed) == 1


def test_get_chat_attachment_not_found(db):
    from routes.chat import get_chat_attachment

    u = User(id=str(uuid.uuid4()), username="att_user", email="att@example.com", first_name="Att", last_name="User", organization="Acme", department="Eng")
    ws = Workspace(id=str(uuid.uuid4()), name="Att WS", slug=f"wsatt-{uuid.uuid4().hex[:6]}", created_by=u.id)
    db.add_all([u, ws])
    db.commit()

    access = WorkspaceAccess(workspace_id=ws.id, user_id=u.id, role="member")
    with pytest.raises(HTTPException) as exc_info:
        get_chat_attachment(attachment_id="nonexistent-att", access=access, db=db)
    assert exc_info.value.status_code == 404


# =========================================================================
# 4. User Chat Settings Route Tests
# =========================================================================

def test_user_chat_settings_crud_lifecycle(db, monkeypatch):
    from routes.chat import get_chat_settings, update_chat_settings, delete_chat_settings
    from schemas.chat import ChatSettingsUpdateRequest

    u = User(id=str(uuid.uuid4()), username="settings_user", email="settings@example.com", first_name="Set", last_name="User", organization="Acme", department="Eng")
    db.add(u)
    db.commit()
    test_user_id = u.id

    fake_request = MagicMock()
    monkeypatch.setattr("routes.chat.caller_id", lambda req: test_user_id)

    # 1. Get default settings
    default_resp = get_chat_settings(request=fake_request, db=db)
    assert default_resp.status_code == 200
    assert default_resp.data.has_api_key is False
    assert default_resp.data.api_key_masked is None

    # 2. Update settings
    update_body = ChatSettingsUpdateRequest(
        provider="google",
        model="gemini-3.8-flash",
        api_key="AIzaSyDummySecretKey12345",
    )
    update_resp = update_chat_settings(body=update_body, request=fake_request, db=db)
    assert update_resp.status_code == 200
    assert update_resp.data.provider == "google"
    assert update_resp.data.model == "gemini-3.8-flash"
    assert update_resp.data.has_api_key is True
    assert "AIza" in update_resp.data.api_key_masked

    # 3. Read back updated settings
    read_resp = get_chat_settings(request=fake_request, db=db)
    assert read_resp.status_code == 200
    assert read_resp.data.has_api_key is True

    # 4. Delete settings
    del_resp = delete_chat_settings(request=fake_request, db=db)
    assert del_resp.status_code == 200
    assert del_resp.data["deleted"] is True

    # 5. Verify reset to default
    after_del_resp = get_chat_settings(request=fake_request, db=db)
    assert after_del_resp.data.has_api_key is False
