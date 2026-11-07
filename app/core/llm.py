"""LiteLLM (OpenAI-compatible Chat Completions) client shared by the AI agent, the document
extraction and the OCR model.

One call = one POST to the configured /v1/chat/completions URL with retries on transient
statuses, a response size limit, and no credentials in the URL. Errors are raised as
LLMError so callers can decide whether a failure blocks the work or only leaves a gap; a
request longer than the model's context window as ContextTooLong, so it can be sent in parts.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any
from urllib.parse import urlparse

import requests

from app.config import settings

logger = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 2_000_000
# How servers (LiteLLM, vLLM, llama.cpp, TGI, OpenAI) refuse a request beyond the context window.
_CONTEXT_OVERFLOW = re.compile(r"context|too long|maximum.{0,40}length|exceed", re.IGNORECASE)


class LLMError(RuntimeError):
    pass


class ContextTooLong(LLMError):
    """The request does not fit into the model's context window: a shorter one may."""


def validate_url(url: str, variable: str = "MAI_LLM_API_URL") -> None:
    parsed = urlparse(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.netloc
            or parsed.username or parsed.password or parsed.fragment):
        raise LLMError(f"адрес {variable} должен быть http(s)-адресом LiteLLM …/v1/chat/completions без логина и пароля.")


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
        raise LLMError(f"не заданы адрес ({url_variable}) или модель.")
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
                if response.status_code in {400, 413, 422}:
                    detail = next(response.iter_content(chunk_size=8192), b"").decode("utf-8", "replace")
                    if _CONTEXT_OVERFLOW.search(detail):
                        raise ContextTooLong("Запрос не помещается в контекстное окно модели.")
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
        raise LLMError(describe_failure(exc, model, timeout, url_variable)) from exc


def describe_failure(exc: Exception, model: str, timeout: float, url_variable: str = "MAI_LLM_API_URL") -> str:
    """Why the call failed, in words for the operator (the callers prefix what was not done)."""
    if isinstance(exc, requests.Timeout):
        return f"модель {model} не ответила за {timeout:.0f} с ({type(exc).__name__})."
    if isinstance(exc, requests.ConnectionError):
        return f"нет соединения с сервером модели ({type(exc).__name__}); проверьте {url_variable}, сеть и прокси."
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        status = exc.response.status_code
        if status in {401, 403}:
            return f"сервер модели отклонил ключ доступа (HTTP {status})."
        if status == 404:
            return f"адрес или модель {model} не найдены на сервере (HTTP 404)."
        if status == 429:
            return "сервер модели перегружен или исчерпан лимит запросов (HTTP 429)."
        if status >= 500:
            return f"ошибка на сервере модели (HTTP {status})."
        return f"сервер модели отклонил запрос (HTTP {status})."
    if isinstance(exc, requests.RequestException):
        return f"запрос к модели не выполнен ({type(exc).__name__})."
    return f"модель вернула ответ в неожиданном формате ({type(exc).__name__})."
