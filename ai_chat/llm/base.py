"""Base definitions and interfaces for the LLM adapter layer."""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class LLMToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    thought_signature: Optional[str] = None


@dataclass
class LLMMessage:
    role: str  # "system", "user", "assistant", "tool"
    content: Optional[str] = None
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[list[LLMToolCall]] = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            d["content"] = self.content
        if self.name is not None:
            d["name"] = self.name
        if self.tool_call_id is not None:
            d["tool_call_id"] = self.tool_call_id
        if self.tool_calls is not None:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": tc.arguments,
                    },
                }
                for tc in self.tool_calls
            ]
        return d


@dataclass
class LLMResult:
    content: Optional[str] = None
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    finish_reason: Optional[str] = None
    usage: dict[str, int] = field(default_factory=dict)
    raw_response: Optional[dict[str, Any]] = None


class BaseLLMAdapter(abc.ABC):
    """Abstract interface for multi-provider LLM integrations."""

    @abc.abstractmethod
    def complete(
        self,
        messages: list[LLMMessage],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> LLMResult:
        """Synchronously send messages and tools to the LLM and receive completion."""
        pass

    @abc.abstractmethod
    async def complete_async(
        self,
        messages: list[LLMMessage],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> LLMResult:
        """Asynchronously send messages and tools to the LLM and receive completion."""
        pass
