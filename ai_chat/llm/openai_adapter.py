"""OpenAI & OpenAI-compatible LLM adapter."""
from __future__ import annotations

import json
from typing import Any, Optional

import httpx

from ai_chat.llm.base import BaseLLMAdapter, LLMMessage, LLMResult, LLMToolCall


class OpenAIAdapter(BaseLLMAdapter):
    """Adapter for OpenAI and any OpenAI-compatible base URL."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o",
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
            # Strip _meta and prepare schema
            params = t.get("parameters", {})
            if isinstance(params, dict):
                params = {k: v for k, v in params.items() if k != "_meta"}
            formatted.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": params,
                },
            })
        return formatted

    def _format_messages(self, messages: list[LLMMessage]) -> list[dict[str, Any]]:
        formatted = []
        for m in messages:
            msg: dict[str, Any] = {"role": m.role}
            if m.attachments and m.role == "user":
                content_parts: list[dict[str, Any]] = []
                for att in m.attachments:
                    if att.data_base64 and att.mime_type.startswith("image/"):
                        content_parts.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{att.mime_type};base64,{att.data_base64}"
                            },
                        })
                    elif att.text_content:
                        fn = att.filename or "Document"
                        content_parts.append({
                            "type": "text",
                            "text": (
                                f"--- Attached Document: {fn} ({att.mime_type}) ---\n"
                                f"{att.text_content}"
                            ),
                        })
                if m.content:
                    content_parts.append({"type": "text", "text": m.content})
                msg["content"] = content_parts
            elif m.content is not None:
                msg["content"] = m.content
            if m.name is not None:
                msg["name"] = m.name
            if m.tool_call_id is not None:
                msg["tool_call_id"] = m.tool_call_id
            if m.tool_calls:
                msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments) if isinstance(tc.arguments, dict) else str(tc.arguments),
                        },
                    }
                    for tc in m.tool_calls
                ]
            formatted.append(msg)
        return formatted

    def _parse_response(self, data: dict[str, Any]) -> LLMResult:
        choices = data.get("choices", [])
        if not choices:
            return LLMResult(content=None, tool_calls=[], finish_reason="stop", raw_response=data)

        choice = choices[0]
        msg = choice.get("message", {})
        content = msg.get("content")
        raw_tool_calls = msg.get("tool_calls", [])

        tool_calls: list[LLMToolCall] = []
        for tc in raw_tool_calls:
            fn = tc.get("function", {})
            fn_name = fn.get("name", "")
            raw_args = fn.get("arguments", "{}")
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except Exception:
                    args = {"_raw": raw_args}
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = {}

            tool_calls.append(
                LLMToolCall(
                    id=tc.get("id", f"call_{len(tool_calls)}"),
                    name=fn_name,
                    arguments=args,
                )
            )

        usage = data.get("usage", {})
        return LLMResult(
            content=content,
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason"),
            usage=usage,
            raw_response=data,
        )

    def _build_payload(
        self,
        messages: list[LLMMessage],
        tools: Optional[list[dict[str, Any]]],
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._format_messages(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        formatted_tools = self._format_tools(tools)
        if formatted_tools:
            payload["tools"] = formatted_tools
            payload["tool_choice"] = "auto"
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
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/chat/completions"
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, headers=headers, json=payload)
            if resp.is_error:
                error_msg = f"HTTP {resp.status_code}"
                try:
                    err_json = resp.json()
                    error_msg = err_json.get("error", {}).get("message") or str(err_json)
                except Exception:
                    error_msg = resp.text[:300]
                raise RuntimeError(f"OpenAI API error ({resp.status_code}): {error_msg}")
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
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/chat/completions"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.is_error:
                error_msg = f"HTTP {resp.status_code}"
                try:
                    err_json = resp.json()
                    error_msg = err_json.get("error", {}).get("message") or str(err_json)
                except Exception:
                    error_msg = resp.text[:300]
                raise RuntimeError(f"OpenAI API error ({resp.status_code}): {error_msg}")
            data = resp.json()
            return self._parse_response(data)
