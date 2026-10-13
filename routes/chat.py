"""Chat routes for the AI Chat Gateway."""
from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ai_chat.gateway import chat_gateway
from ai_chat.llm.settings import (
    delete_user_llm_settings,
    get_user_llm_settings,
    save_user_llm_settings,
)
from ai_chat.registry import TOOL_REGISTRY
from db.session import get_db
from dependencies.workspace import WorkspaceAccess, as_guest, caller_id
from ai_chat.llm.fetcher import discover_provider_models
from models.chat_session import ChatConversation, ChatMessageRecord
from models.user import User
from schemas.chat import (
    ChatRequest,
    ChatResponseData,
    ChatSettingsResponseData,
    ChatSettingsUpdateRequest,
    FetchModelsRequest,
    FetchModelsResponseData,
    ModelInfoItem,
)
from schemas.response import ErrorResponse, SuccessResponse


def sanitize_error_message(msg: str) -> str:
    """Scrub raw API keys and query parameters from error strings."""
    if not msg:
        return ""
    # Scrub key query parameter
    msg = re.sub(r'([?&]key=)[^&\s"\']+', r'\1[REDACTED]', msg)
    # Scrub Bearer tokens
    msg = re.sub(r'(Bearer\s+)[A-Za-z0-9._-]+', r'\1[REDACTED]', msg)
    # Scrub sk- / AIza tokens
    msg = re.sub(r'(sk-[A-Za-z0-9_-]{8,})', r'[REDACTED_KEY]', msg)
    msg = re.sub(r'(AIza[A-Za-z0-9_-]{8,})', r'[REDACTED_KEY]', msg)
    return msg

router = APIRouter(
    prefix="/workspaces/{workspace_id}/chat",
    tags=["AI Chat"],
)

user_chat_router = APIRouter(
    prefix="/user/chat-settings",
    tags=["AI Chat"],
)


@router.post("", response_model=SuccessResponse[ChatResponseData])
def handle_chat_turn(
    body: ChatRequest,
    access: WorkspaceAccess = Depends(as_guest),
    db: Session = Depends(get_db),
):
    """Execute a single conversational turn through the AI Chat Gateway.
    Session-injects workspace_id, enforces RBAC, routes domain tools, and applies T0-T3 policy tiers.
    """
    user = db.query(User).filter(User.id == access.user_id, User.deleted == 0).first()
    if not user:
        raise HTTPException(
            status_code=404,
            detail=ErrorResponse(status_code=404, message="User not found", data={}).model_dump(),
        )

    try:
        result = chat_gateway.execute_turn(
            db=db,
            access=access,
            current_user=user,
            message=body.message,
            conversation_id=body.conversation_id,
            confirm_action_id=body.confirm_action_id,
            confirm_typed_text=body.confirm_typed_text,
            cancel_action_id=body.cancel_action_id,
            undo_action_id=body.undo_action_id,
            context_vars=body.context_vars,
        )
    except Exception as e:
        clean_msg = sanitize_error_message(str(e)) or "An error occurred during chat turn execution"
        raise HTTPException(
            status_code=400,
            detail=ErrorResponse(
                status_code=400,
                message=clean_msg,
                data={},
            ).model_dump(),
        )

    if result.error:
        result.error = sanitize_error_message(result.error)

    if result.error and not result.reply:
        return SuccessResponse(
            status_code=200,
            message="Error encountered during execution",
            data=ChatResponseData(**asdict(result)),
        )

    return SuccessResponse(
        status_code=200,
        message="Turn completed successfully",
        data=ChatResponseData(**asdict(result)),
    )


@router.get("/conversations", response_model=SuccessResponse[list[dict[str, Any]]])
def list_conversations(
    access: WorkspaceAccess = Depends(as_guest),
    db: Session = Depends(get_db),
):
    """List active conversations for the authenticated user in this workspace."""
    convs = (
        db.query(ChatConversation)
        .filter(
            ChatConversation.workspace_id == access.workspace_id,
            ChatConversation.user_id == access.user_id,
            ChatConversation.deleted == 0,
        )
        .order_by(ChatConversation.updated_at.desc())
        .all()
    )
    data = [
        {
            "id": c.id,
            "title": c.title,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        }
        for c in convs
    ]
    return SuccessResponse(status_code=200, message="Conversations retrieved", data=data)


@router.get("/conversations/{conversation_id}", response_model=SuccessResponse[dict[str, Any]])
def get_conversation_history(
    conversation_id: str,
    access: WorkspaceAccess = Depends(as_guest),
    db: Session = Depends(get_db),
):
    """Fetch message history for a specific conversation."""
    conv = (
        db.query(ChatConversation)
        .filter(
            ChatConversation.id == conversation_id,
            ChatConversation.workspace_id == access.workspace_id,
            ChatConversation.user_id == access.user_id,
            ChatConversation.deleted == 0,
        )
        .first()
    )
    if not conv:
        raise HTTPException(
            status_code=404,
            detail=ErrorResponse(status_code=404, message="Conversation not found", data={}).model_dump(),
        )

    messages = (
        db.query(ChatMessageRecord)
        .filter(ChatMessageRecord.conversation_id == conversation_id)
        .order_by(ChatMessageRecord.created_at.asc())
        .all()
    )

    formatted_messages = []
    accumulated_tool_calls: list[Any] = []

    for m in messages:
        if m.role == "tool":
            continue
        if m.role == "assistant":
            if m.tool_calls:
                accumulated_tool_calls.extend(m.tool_calls)
            if not m.content and not m.confirm_card and not m.undo_action:
                continue

            # Attach any accumulated tool calls to this message
            tool_calls_to_show = m.tool_calls or (list(accumulated_tool_calls) if accumulated_tool_calls else None)
            accumulated_tool_calls = []

            formatted_messages.append({
                "id": m.id,
                "role": "assistant",
                "content": m.content,
                "tool_calls": tool_calls_to_show,
                "confirm_card": m.confirm_card,
                "undo_action": m.undo_action,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            })
        else:
            accumulated_tool_calls = []
            formatted_messages.append({
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "tool_calls": m.tool_calls,
                "confirm_card": m.confirm_card,
                "undo_action": m.undo_action,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            })

    data = {
        "conversation": {
            "id": conv.id,
            "title": conv.title,
            "created_at": conv.created_at.isoformat() if conv.created_at else None,
        },
        "messages": formatted_messages,
    }
    return SuccessResponse(status_code=200, message="History retrieved", data=data)


@router.delete("/conversations/{conversation_id}", response_model=SuccessResponse[dict[str, Any]])
def delete_conversation(
    conversation_id: str,
    access: WorkspaceAccess = Depends(as_guest),
    db: Session = Depends(get_db),
):
    """Soft-delete a conversation."""
    conv = (
        db.query(ChatConversation)
        .filter(
            ChatConversation.id == conversation_id,
            ChatConversation.workspace_id == access.workspace_id,
            ChatConversation.user_id == access.user_id,
            ChatConversation.deleted == 0,
        )
        .first()
    )
    if not conv:
        raise HTTPException(
            status_code=404,
            detail=ErrorResponse(status_code=404, message="Conversation not found", data={}).model_dump(),
        )

    conv.deleted = 1
    db.commit()
    return SuccessResponse(status_code=200, message="Conversation deleted", data={"id": conversation_id})


@router.get("/tools", response_model=SuccessResponse[list[dict[str, Any]]])
def list_available_tools(
    access: WorkspaceAccess = Depends(as_guest),
):
    """List exposed tools in the chat gateway with their tier policies and categories."""
    tools = [
        {
            "name": t.name,
            "category": t.domain or t.module,
            "description": t.description,
            "exposure": t.exposure,
            "tier": t.policy.tier,
            "confirm": t.policy.confirm,
            "is_core": t.core,
            "is_virtual": t.is_virtual,
        }
        for t in TOOL_REGISTRY.list_tools()
        if t.exposure == "enabled"
    ]
    return SuccessResponse(status_code=200, message="Tools retrieved", data=tools)


@user_chat_router.get("", response_model=SuccessResponse[ChatSettingsResponseData])
def get_chat_settings(
    request: Request,
    db: Session = Depends(get_db),
):
    """Get the caller's current LLM provider settings."""
    uid = caller_id(request)
    settings = get_user_llm_settings(db, uid)
    if not settings:
        data = ChatSettingsResponseData(
            provider="openai",
            model="gpt-4o",
            has_api_key=False,
            api_key_masked=None,
        )
    else:
        key = settings.get_api_key()
        masked = f"{key[:4]}...{key[-4:]}" if key and len(key) >= 8 else ("****" if key else None)
        data = ChatSettingsResponseData(
            provider=settings.provider,
            model=settings.model,
            base_url=settings.base_url,
            has_api_key=bool(key),
            api_key_masked=masked,
        )
    return SuccessResponse(status_code=200, message="Settings retrieved", data=data)


@user_chat_router.put("", response_model=SuccessResponse[ChatSettingsResponseData])
def update_chat_settings(
    body: ChatSettingsUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Update caller's LLM provider, model, API key, and base URL."""
    uid = caller_id(request)
    settings = save_user_llm_settings(
        db=db,
        user_id=uid,
        provider=body.provider,
        model=body.model,
        api_key=body.api_key,
        base_url=body.base_url,
    )
    key = settings.get_api_key()
    masked = f"{key[:4]}...{key[-4:]}" if key and len(key) >= 8 else ("****" if key else None)
    data = ChatSettingsResponseData(
        provider=settings.provider,
        model=settings.model,
        base_url=settings.base_url,
        has_api_key=bool(key),
        api_key_masked=masked,
    )
    return SuccessResponse(status_code=200, message="Settings updated", data=data)


@user_chat_router.delete("", response_model=SuccessResponse[dict[str, Any]])
def delete_chat_settings(
    request: Request,
    db: Session = Depends(get_db),
):
    """Delete caller's LLM provider settings and credentials."""
    uid = caller_id(request)
    deleted = delete_user_llm_settings(db, uid)
    return SuccessResponse(
        status_code=200,
        message="AI model settings and credentials deleted successfully",
        data={"deleted": deleted},
    )


@user_chat_router.post("/fetch-models", response_model=SuccessResponse[FetchModelsResponseData])
def fetch_available_models(
    body: FetchModelsRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Test credentials and discover real available models from the target provider."""
    uid = caller_id(request)
    api_key = body.api_key
    if not api_key:
        user_settings = get_user_llm_settings(db, uid)
        if user_settings:
            api_key = user_settings.get_api_key()

    try:
        models, default_id = discover_provider_models(
            provider=body.provider,
            api_key=api_key,
            base_url=body.base_url,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=ErrorResponse(
                status_code=400,
                message=str(e),
                data={},
            ).model_dump(),
        )

    model_items = [
        ModelInfoItem(
            id=m.id,
            name=m.name,
            description=m.description,
            is_recommended=m.is_recommended,
        )
        for m in models
    ]

    return SuccessResponse(
        status_code=200,
        message="Available models retrieved successfully",
        data=FetchModelsResponseData(
            provider=body.provider,
            models=model_items,
            default_model=default_id,
        ),
    )
