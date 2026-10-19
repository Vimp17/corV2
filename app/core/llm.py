"""LiteLLM (OpenAI-compatible Chat Completions) client shared by the AI agent, the document
extraction and the OCR model.

One call = one POST to the configured /v1/chat/completions URL with retries on transient
statuses, a response size limit, and no credentials in the URL. Errors are raised as
LLMError so callers can decide whether a failure blocks the work or only leaves a gap.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import urlparse

import requests

from app.config import settings

logger = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 2_000_000


class LLMError(RuntimeError):
    pass


def validate_url(url: str, variable: str = "MAI_LLM_API_URL") -> None:
    parsed = urlparse(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.netloc
            or parsed.username or parsed.password or parsed.fragment):
        raise LLMError(f"{variable} must be an http(s) LiteLLM Chat Completions URL without embedded credentials.")


def extract_content(response_data: Any) -> str:
    if not isinstance(response_data, dict):
        raise ValueError("LLM response must be a JSON object")
    choices = response_data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("LLM response has no choices")
    if not isinstance(choices[0], dict):
        raise ValueError("LLM choice has an unsupported format")
    message = choices[0].get("message", {})
    if not isinstance(message, dict):
        raise ValueError("LLM message has an unsupported format")
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict)).strip()
    raise ValueError("LLM response content is empty or has an unsupported format")


def strip_fence(text: str) -> str:
    """Models often wrap JSON in a ```json fence; return the inner text."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def parse_json_object(text: str) -> dict:
    text = strip_fence(text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        # Some models add a sentence around the object: take the outermost braces.
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("LLM response is not a JSON object") from None
        value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("LLM response is not a JSON object")
    return value


def chat(messages: list[dict], *, model: str, url: str | None = None, api_key: str | None = None,
         timeout: float | None = None, temperature: float = 0.0, max_retries: int | None = None,
         url_variable: str = "MAI_LLM_API_URL", extra: dict | None = None) -> str:
    """Send one chat completion and return the text content of the first choice."""
    url = url if url is not None else settings.llm_api_url
    api_key = api_key if api_key is not None else settings.llm_api_key
    timeout = timeout or settings.llm_timeout_seconds
    max_retries = settings.llm_max_retries if max_retries is None else max_retries
    if not url or not model:
        raise LLMError("LiteLLM URL or model is not configured.")
    validate_url(url, url_variable)
    body = {"model": model, "temperature": temperature, "messages": messages, **(extra or {})}
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        response_data = None
        for attempt in range(max_retries + 1):
            with requests.post(url, headers=headers, json=body, stream=True,
                               timeout=(min(10, timeout), timeout)) as response:
                if response.status_code in {429, 500, 502, 503, 504} and attempt < max_retries:
                    time.sleep(min(2 ** attempt, 4))
                    continue
                response.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_RESPONSE_BYTES:
                        raise ValueError("LLM response exceeded the 2 MB limit")
                    chunks.append(chunk)
                response_data = json.loads(b"".join(chunks).decode("utf-8"))
                break
        return extract_content(response_data)
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        logger.warning("LLM call failed model=%s error_type=%s", model, type(exc).__name__)
        raise LLMError(f"LLM call failed ({type(exc).__name__}).") from exc
