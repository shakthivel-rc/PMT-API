"""Pydantic schemas for AI Chat Gateway requests and responses."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: Optional[str] = Field(default=None, description="User prompt or follow-up message.")
    conversation_id: Optional[str] = Field(default=None, description="Existing conversation UUID if continuing.")
    confirm_action_id: Optional[str] = Field(default=None, description="Confirm token to execute staged T2/T3 card.")
    confirm_typed_text: Optional[str] = Field(default=None, description="Typed entity name/key required for T3 confirmation.")
    cancel_action_id: Optional[str] = Field(default=None, description="Confirm token to cancel staged T2/T3 card.")
    undo_action_id: Optional[str] = Field(default=None, description="Undo token to revert a completed T1 action.")
    context_vars: Optional[dict[str, str]] = Field(default=None, description="Optional override variables (date, timezone, etc.).")


class ChatResponseData(BaseModel):
    conversation_id: str
    reply: Optional[str] = None
    tool_calls_executed: list[dict[str, Any]] = Field(default_factory=list)
    confirm_card: Optional[dict[str, Any]] = None
    undo_action: Optional[dict[str, Any]] = None
    clarification: Optional[dict[str, Any]] = None
    error: Optional[str] = None


class ChatSettingsUpdateRequest(BaseModel):
    provider: str = Field(..., description="openai, anthropic, google, or openai_compatible")
    model: str = Field(..., description="Target model name, e.g. gpt-4o, claude-3-5-sonnet-20241022, gemini-1.5-pro")
    api_key: Optional[str] = Field(default=None, description="Provider API key")
    base_url: Optional[str] = Field(default=None, description="Custom base URL for OpenAI-compatible providers")


class ChatSettingsResponseData(BaseModel):
    provider: str
    model: str
    base_url: Optional[str] = None
    has_api_key: bool
    api_key_masked: Optional[str] = None


class FetchModelsRequest(BaseModel):
    provider: str = Field(..., description="Target provider (openai, anthropic, google, openai_compatible)")
    api_key: Optional[str] = Field(default=None, description="Optional raw key to test. If omitted, uses user saved encrypted key")
    base_url: Optional[str] = Field(default=None, description="Optional custom base URL")


class ModelInfoItem(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    is_recommended: bool = False


class FetchModelsResponseData(BaseModel):
    provider: str
    models: list[ModelInfoItem]
    default_model: str
