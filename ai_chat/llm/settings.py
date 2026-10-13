"""Management and resolution of per-user LLM provider/model/key settings."""
from __future__ import annotations

import os
from typing import Optional

from sqlalchemy.orm import Session

from ai_chat.llm.anthropic_adapter import AnthropicAdapter
from ai_chat.llm.base import BaseLLMAdapter
from ai_chat.llm.google_adapter import GoogleAdapter
from ai_chat.llm.mock_adapter import MockLLMAdapter
from ai_chat.llm.openai_adapter import OpenAIAdapter
from models.user_llm_settings import UserLLMSettings


def get_user_llm_settings(db: Session, user_id: str) -> Optional[UserLLMSettings]:
    """Retrieve saved LLM settings for a specific user."""
    return db.query(UserLLMSettings).filter(UserLLMSettings.user_id == user_id).first()


def save_user_llm_settings(
    db: Session,
    user_id: str,
    provider: str,
    model: str,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> UserLLMSettings:
    """Create or update LLM configuration for a user."""
    settings = get_user_llm_settings(db, user_id)
    if not settings:
        settings = UserLLMSettings(user_id=user_id)
        db.add(settings)

    settings.provider = provider
    settings.model = model
    if api_key is not None:
        settings.set_api_key(api_key)
    if base_url is not None:
        settings.base_url = base_url

    db.commit()
    db.refresh(settings)
    return settings


def delete_user_llm_settings(db: Session, user_id: str) -> bool:
    """Delete saved LLM settings and credentials for a specific user."""
    settings = get_user_llm_settings(db, user_id)
    if settings:
        db.delete(settings)
        db.commit()
        return True
    return False


def build_adapter(
    provider: str,
    model: str,
    api_key: str,
    base_url: Optional[str] = None,
) -> BaseLLMAdapter:
    """Construct an LLM adapter instance for the specified provider and configuration."""
    provider_lower = provider.lower().strip()
    if provider_lower in ("openai", "openai_compatible"):
        kwargs = {"api_key": api_key, "model": model}
        if base_url:
            kwargs["base_url"] = base_url
        return OpenAIAdapter(**kwargs)
    elif provider_lower == "anthropic":
        kwargs = {"api_key": api_key, "model": model}
        if base_url:
            kwargs["base_url"] = base_url
        return AnthropicAdapter(**kwargs)
    elif provider_lower in ("google", "gemini"):
        kwargs = {"api_key": api_key, "model": model}
        if base_url:
            kwargs["base_url"] = base_url
        return GoogleAdapter(**kwargs)
    elif provider_lower == "mock":
        return MockLLMAdapter()
    else:
        raise ValueError(f"Unsupported LLM provider: {provider}")


def resolve_adapter_for_user(
    db: Session,
    user_id: str,
    fallback_mock_in_test: bool = True,
) -> BaseLLMAdapter:
    """Resolve the active LLM adapter for a user, falling back to env vars or mock in tests."""
    user_settings = get_user_llm_settings(db, user_id)
    if user_settings:
        api_key = user_settings.get_api_key()
        if api_key:
            return build_adapter(
                provider=user_settings.provider,
                model=user_settings.model,
                api_key=api_key,
                base_url=user_settings.base_url,
            )

    # Fallback to environment variables
    env_openai_key = os.getenv("OPENAI_API_KEY")
    if env_openai_key:
        return OpenAIAdapter(
            api_key=env_openai_key,
            model=os.getenv("OPENAI_MODEL", "gpt-4o"),
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        )

    env_anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    if env_anthropic_key:
        return AnthropicAdapter(
            api_key=env_anthropic_key,
            model=os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022"),
        )

    env_google_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if env_google_key:
        return GoogleAdapter(
            api_key=env_google_key,
            model=os.getenv("GEMINI_MODEL", "gemini-1.5-pro"),
        )

    if os.getenv("ENVIRONMENT") == "test" and fallback_mock_in_test:
        return MockLLMAdapter()

    raise ValueError(
        "No LLM provider configured. Please configure your API key in Chat Settings or set environment variables."
    )
