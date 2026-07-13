from __future__ import annotations

import logging
from types import TracebackType
from typing import Literal

import anthropic
from anthropic.types import Message
from pydantic import BaseModel, Field

from app.config import CONFIDENCE_THRESHOLD, MODEL
from app.models import Classification, EncodedImage, Rule

logger = logging.getLogger(__name__)


class ClaudeClassification(BaseModel):
    status: Literal["matched", "needs_review"]
    predicted_number: int | None
    confidence: float = Field(ge=0, le=1)
    reason: str
    matched_features: list[str]


class ClaudeClassifier:
    def __init__(self, client=None, threshold: float = CONFIDENCE_THRESHOLD):
        self.client = client or anthropic.AsyncAnthropic()
        self.threshold = threshold

    async def classify(self, encoded: EncodedImage, rules: list[Rule]) -> Classification:
        rule_text = "\n".join(f"{rule.number}: {rule.description}" for rule in rules)
        try:
            response = await self.client.messages.parse(
                model=MODEL,
                max_tokens=1024,
                thinking={"type": "adaptive"},
                output_config={"effort": "high"},
                output_format=ClaudeClassification,
                system=(
                    "Classify one image using only visible subject, composition, background, scene, "
                    "style, color, material, text, angle, and local details. Return matched only when "
                    "one rule is clearly supported; otherwise needs_review. Never infer from filenames."
                ),
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
                            {"type": "text", "text": f"Allowed number rules:\n{rule_text}"},
                        ],
                    }
                ],
            )
            request_id = getattr(response, "_request_id", None)
            if response.stop_reason == "refusal" or response.parsed_output is None:
                return self._failure("refusal", "Claude could not classify this image", request_id)
            parsed = response.parsed_output
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
        except anthropic.AuthenticationError as exc:
            return self._api_failure("authentication", exc)
        except anthropic.PermissionDeniedError as exc:
            return self._api_failure("permission", exc)
        except anthropic.NotFoundError as exc:
            return self._api_failure("model_not_found", exc)
        except anthropic.RateLimitError as exc:
            return self._api_failure("rate_limit", exc)
        except anthropic.APITimeoutError as exc:
            return self._api_failure("timeout", exc)
        except anthropic.APIConnectionError as exc:
            return self._api_failure("connection", exc)
        except anthropic.APIStatusError as exc:
            return self._api_failure("server" if exc.status_code >= 500 else "api", exc)
        except (ValueError, TypeError) as exc:
            if self._parse_error_was_refusal(exc.__traceback__):
                return self._failure("refusal", "Claude could not classify this image")
            logger.warning("Invalid Claude structured response: %s", exc.__class__.__name__)
            return self._failure("invalid_response", "Claude returned an invalid classification")

    @staticmethod
    def _parse_error_was_refusal(traceback: TracebackType | None) -> bool:
        while traceback is not None:
            frame = traceback.tb_frame
            response = frame.f_locals.get("response")
            if (
                frame.f_code.co_name == "parse_response"
                and frame.f_globals.get("__name__") == "anthropic.lib._parse._response"
                and isinstance(response, Message)
                and response.stop_reason == "refusal"
            ):
                return True
            traceback = traceback.tb_next
        return False

    def _api_failure(self, code: str, exc: Exception) -> Classification:
        request_id = getattr(exc, "request_id", None)
        logger.warning("Claude classification failed code=%s request_id=%s", code, request_id)
        return self._failure(code, "Claude API classification failed", request_id)

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
