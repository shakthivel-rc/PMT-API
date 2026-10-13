"""Mock LLM adapter for offline deterministic testing and evaluation."""
from __future__ import annotations

from typing import Any, Callable, List, Optional

from ai_chat.llm.base import BaseLLMAdapter, LLMMessage, LLMResult, LLMToolCall


class MockLLMAdapter(BaseLLMAdapter):
    """A test double adapter that returns pre-configured responses or dynamic callbacks."""

    def __init__(
        self,
        canned_responses: Optional[list[LLMResult]] = None,
        responder: Optional[Callable[[list[LLMMessage], Optional[list[dict[str, Any]]]], LLMResult]] = None,
    ) -> None:
        self.canned_responses = list(canned_responses or [])
        self.responder = responder
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        messages: list[LLMMessage],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> LLMResult:
        self.calls.append({
            "messages": messages,
            "tools": tools,
            "temperature": temperature,
            "max_tokens": max_tokens,
        })
        if self.responder:
            return self.responder(messages, tools)
        if self.canned_responses:
            return self.canned_responses.pop(0)
        return LLMResult(content="Mock response", tool_calls=[])

    async def complete_async(
        self,
        messages: list[LLMMessage],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> LLMResult:
        return self.complete(messages, tools, temperature, max_tokens)
