"""Mock LLM adapter for offline deterministic testing and evaluation."""
from __future__ import annotations

import re
from typing import Any, Callable, List, Optional

from ai_chat.llm.base import BaseLLMAdapter, LLMMessage, LLMResult, LLMToolCall


def _default_mock_responder(messages: list[LLMMessage], tools: Optional[list[dict[str, Any]]]) -> LLMResult:
    # Find the last user message
    last_user_msg = ""
    last_tool_res = ""
    for m in reversed(messages):
        if m.role == "user" and not last_user_msg:
            att_text = ""
            if m.attachments:
                for att in m.attachments:
                    if att.text_content:
                        att_text += f" [Attached: {att.filename or 'file'}: {att.text_content}]"
            last_user_msg = (m.content or "") + att_text
        elif m.role == "tool" and not last_tool_res:
            last_tool_res = m.content or ""

    user_lower = last_user_msg.lower()

    # If there was a tool response immediately preceding, synthesize final answer
    if messages and messages[-1].role == "tool":
        return LLMResult(
            content=f"Processed tool result successfully: {last_tool_res[:100]}...",
            tool_calls=[],
        )

    # Check for space / project creation (T2 action)
    if "create" in user_lower and ("space" in user_lower or "project" in user_lower):
        name_match = re.search(r'named\s+([^,\.]+)', last_user_msg, re.IGNORECASE)
        name = name_match.group(1).strip() if name_match else "Security Vault"
        key_match = re.search(r'key\s+([A-Z0-9]+)', last_user_msg, re.IGNORECASE)
        key = key_match.group(1).strip() if key_match else "SEC"
        return LLMResult(
            content=None,
            tool_calls=[
                LLMToolCall(id="call_mock_space", name="space_create", arguments={"name": name, "key": key})
            ],
        )

    # Check for ticket creation
    if "create" in user_lower and "ticket" in user_lower:
        return LLMResult(
            content=None,
            tool_calls=[
                LLMToolCall(
                    id="call_mock_ticket",
                    name="ticket_create",
                    arguments={"title": "New Ticket from Chat", "priority": "medium"},
                )
            ],
        )

    # Check for spaces/lists/tree query (T0 action)
    if any(k in user_lower for k in ("space", "list", "tree", "workspace structure")):
        return LLMResult(
            content=None,
            tool_calls=[
                LLMToolCall(id="call_mock_tree", name="workspace_tree_get", arguments={})
            ],
        )

    # Check for attachments / deadlock / log analysis
    if "deadlock" in user_lower or "log" in user_lower or "attached" in user_lower:
        return LLMResult(
            content="Analysis of attached log:\n- Issue: DatabaseDeadlockException in order_sync_worker.py\n- Cause: Lock contention during concurrent UPDATE on orders table.\n- Recommendation: Enable retry backoff and order locks consistently.",
            tool_calls=[],
        )

    return LLMResult(
        content=f"Echo from AI Assistant: I understand your request regarding '{last_user_msg}'. How else may I assist?",
        tool_calls=[],
    )


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
        if self.canned_responses:
            return self.canned_responses.pop(0)
        if self.responder:
            return self.responder(messages, tools)
        return _default_mock_responder(messages, tools)

    async def complete_async(
        self,
        messages: list[LLMMessage],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> LLMResult:
        return self.complete(messages, tools, temperature, max_tokens)
