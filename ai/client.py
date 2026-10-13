"""The one place this service calls a model.

Only the AI worker imports this module. The API process never does, so it never holds
`ANTHROPIC_API_KEY` and a slow or failing model can never occupy an API worker — which is
the non-functional requirement docs/design/03-ai-strategy.md is built around.

What every call sends, and why:

* **`fallbacks="default"`** (beta `server-side-fallback-2026-07-01`). Claude's safety
  classifiers can decline a request, and benign engineering text occasionally trips them
  (a security bug report that quotes an exploit string, say). With this set, a declined
  request is re-run server-side on the model Anthropic recommends for that category, in the
  same call, instead of failing the job.
* **Adaptive thinking.** The model decides how much to reason; `AI_EFFORT` bounds it.
* **Structured output** (`output_config.format`). The response is guaranteed to parse
  against the schema, so the worker stores fields, not prose it has to scrape.
* **A 180-second timeout and the SDK's 2 retries.** A summary with adaptive thinking comes
  back in well under a minute; three minutes is the point at which waiting longer is
  indistinguishable from an outage. 3 attempts x 180 s is the ~9 minutes the worker's
  lease (`workers/ai_worker.LEASE_SECONDS`) is sized above.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

import anthropic
from anthropic.types.beta import (
    BetaMessageParam,
    BetaOutputConfigParam,
    BetaTextBlock,
    BetaThinkingConfigAdaptiveParam,
)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
REQUEST_TIMEOUT_SECONDS = 180.0

#: Generous on purpose: `max_tokens` is a ceiling the model is not told about, and hitting it
#: truncates the JSON mid-string. A summary uses a few hundred tokens; the rest is headroom
#: for adaptive thinking, which counts against the same limit.
MAX_TOKENS = 16_000


class AIError(Exception):
    """A model call that did not produce a usable answer.

    `retryable` decides whether the job is put back on the queue with a back-off or marked
    failed. `user_message` is what the person who asked will read, so it names what happened
    and never carries a provider response body, a key or a request id.
    """

    def __init__(self, user_message: str, *, retryable: bool) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.retryable = retryable


@dataclass(frozen=True)
class Completion:
    data: dict[str, Any]
    model: str
    input_tokens: int
    output_tokens: int
    request_id: str | None


class ClaudeJSONClient:
    def __init__(self, model: str, effort: str, client: Any = None) -> None:
        self.model = model
        self.effort = effort
        self._client = client if client is not None else anthropic.Anthropic(
            timeout=REQUEST_TIMEOUT_SECONDS, max_retries=2,
        )

    def complete_json(self, *, system: str, user: str, schema: dict[str, Any]) -> Completion:
        thinking: BetaThinkingConfigAdaptiveParam = {"type": "adaptive"}
        # `effort` is validated against the API's levels at boot (core/config.py), so the
        # cast only narrows a str the settings object has already checked.
        output_config = cast(BetaOutputConfigParam, {
            "effort": self.effort,
            "format": {"type": "json_schema", "schema": schema},
        })
        messages: list[BetaMessageParam] = [{"role": "user", "content": user}]
        try:
            response = self._client.beta.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                thinking=thinking,
                output_config=output_config,
                system=system,
                messages=messages,
            )
        # Most specific first: RateLimitError and InternalServerError are APIStatusErrors,
        # and APITimeoutError is an APIConnectionError.
        except anthropic.RateLimitError as exc:
            raise AIError("The AI service is busy. This will be retried shortly.",
                          retryable=True) from exc
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise AIError("The AI service rejected this deployment's credentials. An "
                          "administrator needs to check the worker's API key.",
                          retryable=False) from exc
        except anthropic.APIStatusError as exc:
            retryable = exc.status_code >= 500 or exc.status_code in (408, 409)
            message = ("The AI service had a temporary problem. This will be retried."
                       if retryable else
                       "The AI service could not process this request.")
            raise AIError(message, retryable=retryable) from exc
        except anthropic.APIConnectionError as exc:
            raise AIError("Could not reach the AI service. This will be retried.",
                          retryable=True) from exc

        # Branch on stop_reason before reading content: a refusal can arrive with an empty
        # content list, and a max_tokens stop leaves the JSON cut off mid-string.
        if response.stop_reason == "refusal":
            raise AIError("The model declined to summarise this thread.", retryable=False)
        if response.stop_reason == "max_tokens":
            raise AIError("The summary was too long to finish. Try again after the thread "
                          "has been trimmed.", retryable=False)

        text = next((block.text for block in response.content
                     if isinstance(block, BetaTextBlock)), None)
        if text is None:
            raise AIError("The model returned no summary.", retryable=True)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AIError("The model returned a summary that could not be read.",
                          retryable=True) from exc

        return Completion(
            data=data,
            model=str(response.model),
            input_tokens=int(response.usage.input_tokens),
            output_tokens=int(response.usage.output_tokens),
            request_id=getattr(response, "_request_id", None),
        )
