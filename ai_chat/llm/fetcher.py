"""Model discovery and credential verification service for LLM providers."""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx
from pydantic import BaseModel


class DiscoveredModel(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    is_recommended: bool = False


CURATED_MODELS: dict[str, list[DiscoveredModel]] = {
    "openai": [
        DiscoveredModel(
            id="gpt-4o",
            name="GPT-4o",
            description="Flagship multimodal model, intelligent & fast",
            is_recommended=True,
        ),
        DiscoveredModel(
            id="gpt-4o-mini",
            name="GPT-4o Mini",
            description="Fast, lightweight, highly cost-effective",
            is_recommended=False,
        ),
        DiscoveredModel(
            id="o1",
            name="OpenAI o1",
            description="Deep reasoning & logic for complex engineering tasks",
            is_recommended=False,
        ),
        DiscoveredModel(
            id="o1-mini",
            name="OpenAI o1 Mini",
            description="Fast reasoning model for math and code",
            is_recommended=False,
        ),
        DiscoveredModel(
            id="o3-mini",
            name="OpenAI o3 Mini",
            description="Next-gen reasoning model",
            is_recommended=False,
        ),
        DiscoveredModel(
            id="gpt-4-turbo",
            name="GPT-4 Turbo",
            description="High capability 128k context window",
            is_recommended=False,
        ),
    ],
    "anthropic": [
        DiscoveredModel(
            id="claude-3-5-sonnet-20241022",
            name="Claude 3.5 Sonnet (v2)",
            description="Industry-leading coding, analysis, and reasoning",
            is_recommended=True,
        ),
        DiscoveredModel(
            id="claude-3-5-haiku-20241022",
            name="Claude 3.5 Haiku",
            description="Ultra-fast, responsive, and economical",
            is_recommended=False,
        ),
        DiscoveredModel(
            id="claude-3-opus-20240229",
            name="Claude 3 Opus",
            description="Deep nuanced reasoning and writing",
            is_recommended=False,
        ),
    ],
    "google": [
        DiscoveredModel(
            id="gemini-1.5-pro",
            name="Gemini 1.5 Pro",
            description="2M token context, superior coding & cross-modal reasoning",
            is_recommended=True,
        ),
        DiscoveredModel(
            id="gemini-1.5-flash",
            name="Gemini 1.5 Flash",
            description="Ultra-fast, lightweight, high throughput",
            is_recommended=False,
        ),
        DiscoveredModel(
            id="gemini-2.0-flash",
            name="Gemini 2.0 Flash",
            description="Next-generation multimodal speed & intelligence",
            is_recommended=False,
        ),
        DiscoveredModel(
            id="gemini-2.0-flash-exp",
            name="Gemini 2.0 Flash Experimental",
            description="Experimental next-generation model",
            is_recommended=False,
        ),
        DiscoveredModel(
            id="gemini-1.5-flash-8b",
            name="Gemini 1.5 Flash-8B",
            description="Compact 8B parameter model for high-frequency requests",
            is_recommended=False,
        ),
    ],
}


def get_curated_models(provider: str) -> list[DiscoveredModel]:
    return CURATED_MODELS.get(provider, CURATED_MODELS["openai"])


def discover_provider_models(
    provider: str,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> tuple[list[DiscoveredModel], str]:
    """Test credentials against provider API and discover real available models.
    Raises ValueError with user-friendly error message if verification fails.
    """
    prov = (provider or "openai").lower()
    if not api_key:
        models = get_curated_models(prov)
        default_id = next((m.id for m in models if m.is_recommended), models[0].id)
        return models, default_id

    # 1. Google Gemini
    if prov == "google":
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
        try:
            with httpx.Client(timeout=12.0) as client:
                res = client.get(url)
        except Exception as e:
            raise ValueError(f"Failed to connect to Google API: {str(e)}")

        if res.status_code != 200:
            try:
                err_body = res.json().get("error", {})
                msg = err_body.get("message") or res.text
            except Exception:
                msg = res.text
            raise ValueError(f"Google Gemini verification failed ({res.status_code}): {msg}")

        data = res.json().get("models", [])
        discovered: list[DiscoveredModel] = []
        for item in data:
            methods = item.get("supportedGenerationMethods", [])
            if "generateContent" not in methods:
                continue
            raw_name = item.get("name", "")
            model_id = raw_name.replace("models/", "")
            display_name = item.get("displayName") or model_id
            description = item.get("description", "")
            is_rec = "1.5-pro" in model_id or "2.0-flash" in model_id
            discovered.append(
                DiscoveredModel(
                    id=model_id,
                    name=display_name,
                    description=description[:120] if description else None,
                    is_recommended=is_rec,
                )
            )

        if not discovered:
            return get_curated_models("google"), "gemini-1.5-pro"

        # Sort so recommended and modern flash/pro models appear first
        discovered.sort(key=lambda m: (not m.is_recommended, "gemini-1.5" not in m.id and "gemini-2.0" not in m.id, m.name))
        return discovered, discovered[0].id

    # 2. OpenAI / OpenAI Compatible
    if prov in ("openai", "openai_compatible"):
        endpoint = base_url.rstrip("/") if base_url else "https://api.openai.com/v1"
        url = f"{endpoint}/models"
        headers = {"Authorization": f"Bearer {api_key}"}

        try:
            with httpx.Client(timeout=12.0) as client:
                res = client.get(url, headers=headers)
        except Exception as e:
            raise ValueError(f"Failed to connect to {endpoint}: {str(e)}")

        if res.status_code != 200:
            try:
                err_body = res.json().get("error", {})
                msg = err_body.get("message") or res.text
            except Exception:
                msg = res.text
            raise ValueError(f"OpenAI verification failed ({res.status_code}): {msg}")

        data = res.json().get("data", [])
        discovered: list[DiscoveredModel] = []
        # Filter for chat / completion models if lots of embeddings/audio exist
        chat_keywords = ("gpt", "o1", "o3", "claude", "llama", "deepseek", "mistral", "qwen", "command")
        for item in data:
            mid = item.get("id", "")
            if not mid:
                continue
            # If standard OpenAI, filter out embeddings/dall-e/tts/moderation/whisper
            if not base_url and any(skip in mid for skip in ("embed", "tts", "whisper", "dall-e", "moderation", "babbage", "davinci", "realtime")):
                continue

            is_rec = mid in ("gpt-4o", "gpt-4o-mini", "o1-mini")
            discovered.append(
                DiscoveredModel(
                    id=mid,
                    name=mid,
                    description=f"Model {mid}",
                    is_recommended=is_rec,
                )
            )

        if not discovered:
            return get_curated_models("openai"), "gpt-4o"

        # Sort: recommended first, then gpt-4/o1/o3, then alphabetical
        def _sort_key(m: DiscoveredModel):
            id_lower = m.id.lower()
            tier = 0 if m.is_recommended else (1 if id_lower.startswith(("gpt-4o", "o1", "o3")) else (2 if "gpt" in id_lower else 3))
            return (tier, id_lower)

        discovered.sort(key=_sort_key)
        return discovered, discovered[0].id

    # 3. Anthropic
    if prov == "anthropic":
        url = "https://api.anthropic.com/v1/models"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
        try:
            with httpx.Client(timeout=12.0) as client:
                res = client.get(url, headers=headers)
        except Exception as e:
            raise ValueError(f"Failed to connect to Anthropic API: {str(e)}")

        if res.status_code == 200:
            data = res.json().get("data", [])
            discovered = [
                DiscoveredModel(
                    id=item.get("id"),
                    name=item.get("display_name") or item.get("id"),
                    description=f"Anthropic {item.get('display_name') or item.get('id')}",
                    is_recommended="3-5-sonnet" in item.get("id", ""),
                )
                for item in data
                if item.get("id")
            ]
            if discovered:
                discovered.sort(key=lambda m: (not m.is_recommended, m.name))
                return discovered, discovered[0].id
        elif res.status_code in (401, 403):
            try:
                err_body = res.json().get("error", {})
                msg = err_body.get("message") or res.text
            except Exception:
                msg = res.text
            raise ValueError(f"Anthropic verification failed ({res.status_code}): {msg}")

        # If Anthropic returned 404 for /models (older API account), fallback to curated
        return get_curated_models("anthropic"), "claude-3-5-sonnet-20241022"

    return get_curated_models(prov), "gpt-4o"
