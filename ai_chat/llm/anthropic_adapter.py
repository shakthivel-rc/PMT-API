"""Anthropic Claude LLM adapter."""
from __future__ import annotations

import json
from typing import Any, Optional

import httpx

from ai_chat.llm.base import BaseLLMAdapter, LLMMessage, LLMResult, LLMToolCall


class AnthropicAdapter(BaseLLMAdapter):
    """Adapter for Anthropic Claude Messages API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anthropic.com/v1",
        model: str = "claude-3-5-sonnet-20241022",
        timeout: float = 60.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def _format_tools(self, tools: Optional[list[dict[str, Any]]]) -> Optional[list[dict[str, Any]]]:
        if not tools:
            return None
        formatted = []
        for t in tools:
            params = t.get("parameters", {})
            if isinstance(params, dict):
                params = {k: v for k, v in params.items() if k != "_meta"}
            formatted.append({
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": params,
            })
        return formatted

    def _format_messages_and_system(
        self, messages: list[LLMMessage]
    ) -> tuple[Optional[str], list[dict[str, Any]]]:
        system_prompt: Optional[str] = None
        formatted: list[dict[str, Any]] = []

        for m in messages:
            if m.role == "system":
                # Anthropic expects system prompt at top-level
                if system_prompt:
                    system_prompt += f"\n\n{m.content or ''}"
                else:
                    system_prompt = m.content or ""
                continue

            if m.role == "tool":
                # In Anthropic, tool execution results are represented as user role blocks
                formatted.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": m.tool_call_id or "",
                            "content": m.content or "",
                        }
                    ],
                })
                continue

            if m.role == "assistant":
                content_blocks: list[dict[str, Any]] = []
                if m.content:
                    content_blocks.append({"type": "text", "text": m.content})
                if m.tool_calls:
                    for tc in m.tool_calls:
                        content_blocks.append({
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        })
                formatted.append({
                    "role": "assistant",
                    "content": content_blocks if content_blocks else "",
                })
                continue

            # Standard user message
            if m.attachments:
                u_blocks: list[dict[str, Any]] = []
                for att in m.attachments:
                    if att.data_base64 and att.mime_type.startswith("image/"):
                        u_blocks.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": att.mime_type,
                                "data": att.data_base64,
                            },
                        })
                    elif att.text_content:
                        fn = att.filename or "Document"
                        u_blocks.append({
                            "type": "text",
                            "text": (
                                f"--- Attached Document: {fn} ({att.mime_type}) ---\n"
                                f"{att.text_content}"
                            ),
                        })
                if m.content:
                    u_blocks.append({"type": "text", "text": m.content})
                formatted.append({
                    "role": "user",
                    "content": u_blocks,
                })
            else:
                formatted.append({
                    "role": "user",
                    "content": m.content or "",
                })

        return system_prompt, formatted

    def _parse_response(self, data: dict[str, Any]) -> LLMResult:
        content_text_parts: list[str] = []
        tool_calls: list[LLMToolCall] = []

        for block in data.get("content", []):
            b_type = block.get("type")
            if b_type == "text":
                content_text_parts.append(block.get("text", ""))
            elif b_type == "tool_use":
                raw_input = block.get("input", {})
                tool_calls.append(
                    LLMToolCall(
                        id=block.get("id", f"call_{len(tool_calls)}"),
                        name=block.get("name", ""),
                        arguments=raw_input if isinstance(raw_input, dict) else {},
                    )
                )

        full_content = "".join(content_text_parts).strip() if content_text_parts else None
        usage = data.get("usage", {})

        return LLMResult(
            content=full_content,
            tool_calls=tool_calls,
            finish_reason=data.get("stop_reason"),
            usage={
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
            },
            raw_response=data,
        )

    def _build_payload(
        self,
        messages: list[LLMMessage],
        tools: Optional[list[dict[str, Any]]],
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        system_prompt, formatted_messages = self._format_messages_and_system(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": formatted_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system_prompt:
            payload["system"] = system_prompt
        formatted_tools = self._format_tools(tools)
        if formatted_tools:
            payload["tools"] = formatted_tools
        return payload

    def complete(
        self,
        messages: list[LLMMessage],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> LLMResult:
        payload = self._build_payload(messages, tools, temperature, max_tokens)
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        url = f"{self.base_url}/messages"
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, headers=headers, json=payload)
            if resp.is_error:
                error_msg = f"HTTP {resp.status_code}"
                try:
                    err_json = resp.json()
                    error_msg = err_json.get("error", {}).get("message") or str(err_json)
                except Exception:
                    error_msg = resp.text[:300]
                raise RuntimeError(f"Anthropic API error ({resp.status_code}): {error_msg}")
            data = resp.json()
            return self._parse_response(data)

    async def complete_async(
        self,
        messages: list[LLMMessage],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> LLMResult:
        payload = self._build_payload(messages, tools, temperature, max_tokens)
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        url = f"{self.base_url}/messages"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.is_error:
                error_msg = f"HTTP {resp.status_code}"
                try:
                    err_json = resp.json()
                    error_msg = err_json.get("error", {}).get("message") or str(err_json)
                except Exception:
                    error_msg = resp.text[:300]
                raise RuntimeError(f"Anthropic API error ({resp.status_code}): {error_msg}")
            data = resp.json()
            return self._parse_response(data)
