from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.config import API_BASE_URL, API_KEY, CONFIDENCE_THRESHOLD, MODEL
from app.models import Classification, EncodedImage, Rule

logger = logging.getLogger(__name__)

_JSON_BLOCK = re.compile(r"\{[\s\S]*\}")


class ClaudeClassification(BaseModel):
    status: Literal["matched", "needs_review"]
    predicted_number: int | None
    confidence: float = Field(ge=0, le=1)
    reason: str
    matched_features: list[str]


class ClaudeClassifier:
    """Image classifier via OpenAI-compatible chat completions (vision)."""

    def __init__(
        self,
        client=None,
        threshold: float = CONFIDENCE_THRESHOLD,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ):
        # `client` is reserved for tests that inject a mock with .classify_raw / .messages
        self.client = client
        self.threshold = threshold
        self.base_url = (base_url or API_BASE_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else API_KEY
        self.model = model or MODEL

    async def classify(self, encoded: EncodedImage, rules: list[Rule]) -> Classification:
        rule_text = "\n".join(f"{rule.number}: {rule.description}" for rule in rules)
        system = (
            "Classify one image using only visible subject, composition, background, scene, "
            "style, color, material, text, angle, and local details. Return matched only when "
            "one rule is clearly supported; otherwise needs_review. Never infer from filenames. "
            "Respond with a single JSON object only, no markdown, with keys: "
            'status ("matched" or "needs_review"), predicted_number (int or null), '
            "confidence (0-1 float), reason (string), matched_features (string array)."
        )
        user_text = f"Allowed number rules:\n{rule_text}"

        try:
            if self.client is not None and hasattr(self.client, "messages"):
                # Legacy Anthropic-style mock used by unit tests
                return await self._classify_via_anthropic_mock(encoded, rules, system, user_text)

            raw, request_id = await self._chat_completion(encoded, system, user_text)
            parsed = self._parse_json(raw)
            return self._to_classification(parsed, rules, request_id)
        except httpx.TimeoutException as exc:
            return self._api_failure("timeout", exc)
        except httpx.ConnectError as exc:
            return self._api_failure("connection", exc)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (401, 403):
                code = "authentication" if status == 401 else "permission"
            elif status == 404:
                code = "model_not_found"
            elif status == 429:
                code = "rate_limit"
            elif status >= 500:
                code = "server"
            else:
                code = "api"
            return self._api_failure(code, exc)
        except (ValidationError, ValueError, TypeError, json.JSONDecodeError, KeyError) as exc:
            logger.warning("Invalid model structured response: %s: %s", exc.__class__.__name__, exc)
            return self._failure("invalid_response", "Model returned an invalid classification")

    async def _chat_completion(
        self, encoded: EncodedImage, system: str, user_text: str
    ) -> tuple[str, str | None]:
        if not self.api_key:
            raise httpx.HTTPStatusError(
                "Missing API key",
                request=httpx.Request("POST", f"{self.base_url}/chat/completions"),
                response=httpx.Response(401, request=httpx.Request("POST", f"{self.base_url}/chat/completions")),
            )

        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{encoded.media_type};base64,{encoded.data}",
                            },
                        },
                        {"type": "text", "text": user_text},
                    ],
                },
            ],
            "response_format": {"type": "json_object"},
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            # Some gateways reject response_format; retry without it
            if response.status_code == 400 and "response_format" in response.text.lower():
                payload.pop("response_format", None)
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )
            response.raise_for_status()
            body = response.json()

        request_id = body.get("id") or response.headers.get("x-request-id")
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"Unexpected completion shape: {body!r}") from exc

        if isinstance(content, list):
            # Some providers return content parts
            texts = [
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") in (None, "text")
            ]
            content = "\n".join(t for t in texts if t)

        if not isinstance(content, str) or not content.strip():
            raise ValueError("Empty model content")

        return content, request_id

    def _parse_json(self, raw: str) -> ClaudeClassification:
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            match = _JSON_BLOCK.search(text)
            if not match:
                raise
            data = json.loads(match.group(0))
        return ClaudeClassification.model_validate(data)

    def _to_classification(
        self,
        parsed: ClaudeClassification,
        rules: list[Rule],
        request_id: str | None,
    ) -> Classification:
        valid_numbers = {rule.number for rule in rules}
        valid = (
            parsed.status == "matched"
            and parsed.predicted_number in valid_numbers
            and parsed.confidence >= self.threshold
        )
        if not valid:
            return Classification(
                status="needs_review",
                predicted_number=None,
                confidence=parsed.confidence,
                reason=parsed.reason,
                matched_features=tuple(parsed.matched_features),
                request_id=request_id,
            )
        return Classification(
            status="matched",
            predicted_number=parsed.predicted_number,
            confidence=parsed.confidence,
            reason=parsed.reason,
            matched_features=tuple(parsed.matched_features),
            request_id=request_id,
        )

    async def _classify_via_anthropic_mock(
        self,
        encoded: EncodedImage,
        rules: list[Rule],
        system: str,
        user_text: str,
    ) -> Classification:
        """Support unit tests that inject an Anthropic-like client.messages.parse mock."""
        response = await self.client.messages.parse(
            model=self.model,
            max_tokens=1024,
            output_format=ClaudeClassification,
            system=system,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": encoded.media_type,
                                "data": encoded.data,
                            },
                        },
                        {"type": "text", "text": user_text},
                    ],
                }
            ],
        )
        request_id = getattr(response, "_request_id", None)
        if getattr(response, "stop_reason", None) == "refusal" or response.parsed_output is None:
            return self._failure("refusal", "Model could not classify this image", request_id)
        return self._to_classification(response.parsed_output, rules, request_id)

    def _api_failure(self, code: str, exc: Exception) -> Classification:
        request_id = getattr(exc, "request_id", None)
        if request_id is None and isinstance(exc, httpx.HTTPStatusError):
            request_id = exc.response.headers.get("x-request-id")
        logger.warning("Model classification failed code=%s request_id=%s err=%s", code, request_id, exc)
        return self._failure(code, "Model API classification failed", request_id)

    @staticmethod
    def _failure(code: str, reason: str, request_id: str | None = None) -> Classification:
        return Classification(
            status="needs_review",
            predicted_number=None,
            confidence=0,
            reason=reason,
            error_code=code,
            request_id=request_id,
        )
