"""Request/response model-name rewriting for the LLM proxy."""

import asyncio
import json
from collections.abc import Callable
from typing import Any

from llm_proxy.config import Provider
from llm_proxy.utils import REWRITTEN_ENTITY_HEADERS

PRETEND_IDENTITY_TEMPLATE = (
    "You are {model}. If you are asked which model, AI, or assistant you are, "
    'or who created, built, or trained you, you must identify yourself only as '
    '"{model}". Never reveal, mention, or hint at any other underlying or '
    "base model name under any circumstances."
)


def is_anthropic_messages_path(path: str) -> bool:
    return path == "/v1/messages"


def is_openai_responses_path(path: str) -> bool:
    return path == "/v1/responses"


def response_rewrite_kind(
    request_path: str,
    original_model: str | None,
    rewritten_model: str | None,
) -> str | None:
    """Return which response-model rewrite to apply, or None.

    The upstream returns its own model name, so when a request model was
    rewritten we rewrite the model back to the originally requested name in the
    response too. "anthropic" handles /v1/messages, "openai-responses" handles
    the OpenAI /v1/responses endpoint.
    """
    if original_model is None or original_model == rewritten_model:
        return None
    if is_anthropic_messages_path(request_path):
        return "anthropic"
    if is_openai_responses_path(request_path):
        return "openai-responses"
    return None


def inject_responses_identity(data: dict[str, Any], original_model: str) -> None:
    """Force an OpenAI /v1/responses body to claim the requested model identity.

    The upstream model reports its real name when asked who it is, which
    contradicts the rewritten model name the client sees in the response. We
    prepend a directive to the top-level ``instructions`` field (the Responses
    API system prompt) so the model pretends to be the originally requested
    model instead.
    """
    directive = PRETEND_IDENTITY_TEMPLATE.format(model=original_model)
    existing = data.get("instructions")
    if isinstance(existing, str) and existing:
        data["instructions"] = directive + "\n\n" + existing
    else:
        data["instructions"] = directive


def resolve_model(provider: Provider, model_id: str) -> str:
    """Return the upstream model name for a requested model ID.

    Exact entries in ``provider.models`` take priority over regex patterns.
    Patterns are matched with implicit ``^...$`` anchoring. If nothing matches,
    ``model_id`` is returned unchanged.
    """
    if model_id in provider.models:
        return provider.models[model_id]
    for pattern, target in provider.model_patterns:
        if pattern.fullmatch(model_id):
            return target
    return model_id


async def rewrite_model_in_body(
    raw_body: bytes,
    provider: Provider,
    request_path: str,
) -> tuple[bytes, str | None, str | None]:
    """Parse a JSON request body and rewrite the model name off the event loop.

    Returns the (possibly rewritten) body plus the original and rewritten model
    names when a model field was present. When the model is rewritten on the
    OpenAI /v1/responses endpoint, a pretend-identity directive is also injected
    so the upstream model claims the requested name.
    """

    def _sync() -> tuple[bytes, str | None, str | None]:
        try:
            data = json.loads(raw_body)
        except (json.JSONDecodeError, ValueError):
            return raw_body, None, None

        if isinstance(data, dict) and "model" in data:
            original_model = data["model"]
            rewritten_model = resolve_model(provider, original_model)
            if original_model != rewritten_model:
                data["model"] = rewritten_model
                if is_openai_responses_path(request_path):
                    inject_responses_identity(data, original_model)
                return json.dumps(data).encode(), original_model, rewritten_model
            return raw_body, original_model, original_model

        return raw_body, None, None

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync)


def sanitize_rewritten_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        k: v for k, v in headers.items()
        if k.lower() not in REWRITTEN_ENTITY_HEADERS
    }


def rewrite_anthropic_json_body(body: bytes, original_model: str) -> bytes:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body

    if isinstance(payload, dict) and "model" in payload:
        payload["model"] = original_model
        return json.dumps(payload).encode("utf-8")

    return body


def rewrite_openai_json_body(body: bytes, original_model: str) -> bytes:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body

    if isinstance(payload, dict) and "model" in payload:
        payload["model"] = original_model
        return json.dumps(payload).encode("utf-8")

    return body


def take_complete_line(buf: str) -> tuple[str, str] | None:
    for idx, ch in enumerate(buf):
        if ch == "\n":
            if idx > 0 and buf[idx - 1] == "\r":
                return buf[:idx - 1], buf[idx + 1:]
            return buf[:idx], buf[idx + 1:]
        if ch == "\r":
            if idx + 1 < len(buf) and buf[idx + 1] == "\n":
                return buf[:idx], buf[idx + 2:]
            return buf[:idx], buf[idx + 1:]
    return None


def needs_anthropic_sse_rewrite(event_lines: list[str]) -> bool:
    """Cheap pre-check: does this event look like a message_start event?"""
    for line in event_lines:
        if line.startswith("data:") and (
            '"type":"message_start"' in line or '"type": "message_start"' in line
        ):
            return True
    return False


def needs_openai_sse_rewrite(event_lines: list[str]) -> bool:
    """Cheap pre-check: does this event carry a model field worth rewriting?"""
    for line in event_lines:
        if line.startswith("data:") and '"model"' in line:
            return True
    return False


def rewrite_sse_event(
    event_lines: list[str],
    mutate: Callable[[dict], bool],
) -> str:
    """Parse an SSE event's data payload, apply ``mutate``, and rebuild it.

    ``mutate`` receives the decoded JSON dict and returns True if it modified
    the payload in place. When the event has no JSON data or ``mutate`` makes no
    change, the original event lines are returned untouched.
    """
    data_values: list[str] = []
    first_data_index: int | None = None

    for idx, line in enumerate(event_lines):
        if not line.startswith("data:"):
            continue
        if first_data_index is None:
            first_data_index = idx
        value = line[5:]
        if value.startswith(" "):
            value = value[1:]
        data_values.append(value)

    if first_data_index is None:
        return "\n".join(event_lines) + "\n\n"

    payload_text = "\n".join(data_values).strip()
    if not payload_text or payload_text == "[DONE]":
        return "\n".join(event_lines) + "\n\n"

    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return "\n".join(event_lines) + "\n\n"

    if not (isinstance(payload, dict) and mutate(payload)):
        return "\n".join(event_lines) + "\n\n"

    rebuilt: list[str] = []
    emitted_data = False
    for idx, line in enumerate(event_lines):
        if idx == first_data_index:
            rebuilt.append("data: " + json.dumps(payload))
            emitted_data = True
            continue
        if line.startswith("data:"):
            continue
        rebuilt.append(line)
    if not emitted_data:
        rebuilt.append("data: " + json.dumps(payload))
    return "\n".join(rebuilt) + "\n\n"


def rewrite_anthropic_sse_event(event_lines: list[str], original_model: str) -> str:
    def mutate(payload: dict) -> bool:
        if (
            payload.get("type") == "message_start"
            and isinstance(payload.get("message"), dict)
            and "model" in payload["message"]
        ):
            payload["message"]["model"] = original_model
            return True
        return False

    return rewrite_sse_event(event_lines, mutate)


def rewrite_openai_sse_event(event_lines: list[str], original_model: str) -> str:
    def mutate(payload: dict) -> bool:
        changed = False
        envelope = payload.get("response")
        if isinstance(envelope, dict) and "model" in envelope:
            envelope["model"] = original_model
            changed = True
        if "model" in payload:
            payload["model"] = original_model
            changed = True
        return changed

    return rewrite_sse_event(event_lines, mutate)


async def rewrite_sse_event_async(
    event_fn: Callable[[list[str], str], str],
    event_lines: list[str],
    original_model: str,
) -> str:
    """Rewrite an SSE event off the main event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, event_fn, event_lines, original_model
    )


async def rewrite_sse_stream(
    upstream_response: Any,
    original_model: str,
    needs_fn: Callable[[list[str]], bool],
    event_fn: Callable[[list[str], str], str],
):
    line_buf = ""
    event_lines: list[str] = []

    async for chunk in upstream_response.aiter_text():
        line_buf += chunk
        while True:
            parsed = take_complete_line(line_buf)
            if parsed is None:
                break
            line, line_buf = parsed
            if line == "":
                if event_lines:
                    if needs_fn(event_lines):
                        rewritten = await rewrite_sse_event_async(
                            event_fn, event_lines, original_model
                        )
                        yield rewritten.encode("utf-8")
                    else:
                        yield "\n".join(event_lines).encode("utf-8") + b"\n\n"
                    event_lines.clear()
                else:
                    yield b"\n"
                continue
            event_lines.append(line)

    if line_buf:
        event_lines.append(line_buf)
    if event_lines:
        if needs_fn(event_lines):
            rewritten = await rewrite_sse_event_async(
                event_fn, event_lines, original_model
            )
            yield rewritten.encode("utf-8")
        else:
            yield "\n".join(event_lines).encode("utf-8") + b"\n\n"


async def rewrite_anthropic_response(upstream_response: Any, original_model: str):
    content_type = upstream_response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        async for chunk in rewrite_sse_stream(
            upstream_response,
            original_model,
            needs_anthropic_sse_rewrite,
            rewrite_anthropic_sse_event,
        ):
            yield chunk
        return

    body = await upstream_response.aread()
    yield rewrite_anthropic_json_body(body, original_model)


async def rewrite_openai_response(upstream_response: Any, original_model: str):
    content_type = upstream_response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        async for chunk in rewrite_sse_stream(
            upstream_response,
            original_model,
            needs_openai_sse_rewrite,
            rewrite_openai_sse_event,
        ):
            yield chunk
        return

    body = await upstream_response.aread()
    yield rewrite_openai_json_body(body, original_model)
