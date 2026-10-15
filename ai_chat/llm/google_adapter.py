"""Google Gemini LLM adapter."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

import httpx

from ai_chat.llm.base import BaseLLMAdapter, LLMMessage, LLMResult, LLMToolCall


def _clean_gemini_schema(schema: Any) -> Any:
    """Recursively cleans a JSON schema dict to conform to Google Gemini Schema proto.
    Removes unsupported keys like additionalProperties, $schema, default, etc.,
    and ensures types are uppercase.
    """
    if not isinstance(schema, dict):
        return schema

    allowed_keys = {
        "type",
        "format",
        "description",
        "nullable",
        "enum",
        "properties",
        "required",
        "items",
    }
    cleaned: dict[str, Any] = {}
    for k, v in schema.items():
        if k in allowed_keys:
            if k == "properties" and isinstance(v, dict):
                cleaned[k] = {
                    prop_name: _clean_gemini_schema(prop_val)
                    for prop_name, prop_val in v.items()
                    if isinstance(prop_val, dict)
                }
            elif k == "items" and isinstance(v, dict):
                cleaned[k] = _clean_gemini_schema(v)
            elif k == "type" and isinstance(v, str):
                cleaned[k] = v.upper()
            else:
                cleaned[k] = v

    if "type" not in cleaned:
        cleaned["type"] = "OBJECT"
    return cleaned


class GoogleAdapter(BaseLLMAdapter):
    """Adapter for Google Gemini REST API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        model: str = "gemini-1.5-pro",
        timeout: float = 60.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def _format_tools(self, tools: Optional[list[dict[str, Any]]]) -> Optional[list[dict[str, Any]]]:
        if not tools:
            return None
        declarations = []
        for t in tools:
            raw_params = t.get("parameters", {})
            cleaned_params = _clean_gemini_schema(raw_params) if isinstance(raw_params, dict) else {"type": "OBJECT"}
            declarations.append({
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": cleaned_params,
            })
        return [{"functionDeclarations": declarations}]

    def _format_messages_and_system(
        self, messages: list[LLMMessage]
    ) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]]]:
        system_instruction: Optional[dict[str, Any]] = None
        contents: list[dict[str, Any]] = []

        for m in messages:
            if m.role == "system":
                system_instruction = {"parts": [{"text": m.content or ""}]}
                continue

            if m.role == "tool":
                contents.append({
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": m.name or "tool_result",
                                "response": {"result": m.content or ""},
                            }
                        }
                    ],
                })
                continue

            if m.role == "assistant":
                parts: list[dict[str, Any]] = []
                if m.content:
                    parts.append({"text": m.content})
                if m.tool_calls:
                    for tc in m.tool_calls:
                        part: dict[str, Any] = {
                            "functionCall": {
                                "name": tc.name,
                                "args": tc.arguments,
                            }
                        }
                        if tc.thought_signature:
                            part["thoughtSignature"] = tc.thought_signature
                        parts.append(part)
                contents.append({
                    "role": "model",
                    "parts": parts,
                })
                continue

            # Standard user message
            contents.append({
                "role": "user",
                "parts": [{"text": m.content or ""}],
            })

        return system_instruction, contents

    def _parse_response(self, data: dict[str, Any]) -> LLMResult:
        candidates = data.get("candidates", [])
        if not candidates:
            return LLMResult(content=None, tool_calls=[], finish_reason="stop", raw_response=data)

        cand = candidates[0]
        content_obj = cand.get("content", {})
        parts = content_obj.get("parts", [])

        content_texts: list[str] = []
        tool_calls: list[LLMToolCall] = []

        for p in parts:
            if "text" in p:
                content_texts.append(p["text"])
            elif "functionCall" in p:
                fc = p["functionCall"]
                ts = p.get("thoughtSignature")
                tool_calls.append(
                    LLMToolCall(
                        id=f"call_{len(tool_calls)}",
                        name=fc.get("name", ""),
                        arguments=fc.get("args", {}),
                        thought_signature=ts,
                    )
                )

        usage_meta = data.get("usageMetadata", {})
        usage = {
            "prompt_tokens": usage_meta.get("promptTokenCount", 0),
            "completion_tokens": usage_meta.get("candidatesTokenCount", 0),
            "total_tokens": usage_meta.get("totalTokenCount", 0),
        }

        full_content = "".join(content_texts).strip() if content_texts else None
        return LLMResult(
            content=full_content,
            tool_calls=tool_calls,
            finish_reason=cand.get("finishReason"),
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
        system_instruction, contents = self._format_messages_and_system(messages)
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_instruction:
            payload["systemInstruction"] = system_instruction
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
        url = f"{self.base_url}/models/{self.model}:generateContent"
        params = {"key": self.api_key}
        headers = {"Content-Type": "application/json"}
        max_retries = 3
        backoff = 1.0
        with httpx.Client(timeout=self.timeout) as client:
            for attempt in range(max_retries):
                resp = client.post(url, params=params, headers=headers, json=payload)
                if resp.is_error:
                    if resp.status_code in (503, 429) and attempt < max_retries - 1:
                        time.sleep(backoff)
                        backoff *= 2
                        continue
                    error_msg = f"HTTP {resp.status_code}"
                    try:
                        err_json = resp.json()
                        error_msg = err_json.get("error", {}).get("message") or str(err_json)
                    except Exception:
                        error_msg = resp.text[:300]
                    raise RuntimeError(f"Google Gemini API error ({resp.status_code}): {error_msg}")
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
        url = f"{self.base_url}/models/{self.model}:generateContent"
        params = {"key": self.api_key}
        headers = {"Content-Type": "application/json"}
        max_retries = 3
        backoff = 1.0
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(max_retries):
                resp = await client.post(url, params=params, headers=headers, json=payload)
                if resp.is_error:
                    if resp.status_code in (503, 429) and attempt < max_retries - 1:
                        await asyncio.sleep(backoff)
                        backoff *= 2
                        continue
                    error_msg = f"HTTP {resp.status_code}"
                    try:
                        err_json = resp.json()
                        error_msg = err_json.get("error", {}).get("message") or str(err_json)
                    except Exception:
                        error_msg = resp.text[:300]
                    raise RuntimeError(f"Google Gemini API error ({resp.status_code}): {error_msg}")
                data = resp.json()
                return self._parse_response(data)
