"""Core model proxy handler and provider resolution."""

import logging
import re

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from llm_proxy.config import Provider
from llm_proxy.rewriter import (
    response_rewrite_kind,
    rewrite_anthropic_response,
    rewrite_model_in_body,
    rewrite_openai_response,
    sanitize_rewritten_headers,
)
from llm_proxy.utils import HOP_BY_HOP

LOGGER = logging.getLogger("llm_proxy")

_PROVIDER_PREFIX_RE = re.compile(r"^\[([a-zA-Z0-9_-]+)\]")

# Characters commonly found in concrete model IDs. Keys in ``models`` that
# contain anything outside this set are treated as regex patterns and are not
# advertised in /v1/models.
_MODEL_ID_LITERAL_RE = re.compile(r"^[a-zA-Z0-9._:-]+$")


def split_auth_token(auth: str) -> tuple[str | None, str]:
    match = _PROVIDER_PREFIX_RE.match(auth)
    if not match:
        return None, auth
    return match.group(1), auth[match.end():]


def resolve_provider(request: Request, providers: dict[str, Provider]) -> Provider | None:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:].strip()
    identifier, _ = split_auth_token(token)
    if identifier is None:
        return None
    return providers.get(identifier)


def models_payload(provider: Provider) -> dict:
    advertised = [
        model_name
        for model_name in provider.models
        if _MODEL_ID_LITERAL_RE.match(model_name)
    ]
    return {
        "data": [
            {
                "id": model_name,
                "type": "model",
                "display_name": model_name,
                "created_at": "2025-01-01T00:00:00Z",
            }
            for model_name in advertised
        ],
        "has_more": False,
    }


async def handle_list_models(
    request: Request,
    providers: dict[str, Provider],
) -> JSONResponse:
    provider = resolve_provider(request, providers)
    if provider is None:
        return JSONResponse(
            {"error": {"message": "Invalid API key"}},
            status_code=401,
        )
    return JSONResponse(models_payload(provider))


async def handle_proxy_request(
    request: Request,
    provider: Provider,
    model_client: httpx.AsyncClient,
) -> StreamingResponse:
    raw_body = await request.body()
    original_model: str | None = None
    rewritten_model: str | None = None

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type and raw_body:
        raw_body, original_model, rewritten_model = await rewrite_model_in_body(
            raw_body, provider, request.url.path
        )
        if original_model is not None:
            LOGGER.info(
                "model rewrite check path=%s inbound_model=%r outbound_model=%r",
                request.url.path,
                original_model,
                rewritten_model,
            )

    upstream_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP
    }
    upstream_headers["host"] = httpx.URL(provider.upstream_url).host

    auth = request.headers.get("authorization", "")
    token = auth[7:].strip()
    _, real_key = split_auth_token(token)
    upstream_headers["authorization"] = f"Bearer {real_key}"

    x_api_key = request.headers.get("x-api-key", "")
    if x_api_key:
        _, real_x_api_key = split_auth_token(x_api_key)
        upstream_headers["x-api-key"] = real_x_api_key

    rewrite_kind = response_rewrite_kind(
        request.url.path, original_model, rewritten_model
    )
    if rewrite_kind is not None:
        upstream_headers["accept-encoding"] = "identity"

    target = provider.upstream_url + request.url.path
    if request.url.query:
        target += "?" + request.url.query

    req = httpx.Request(
        method=request.method,
        url=target,
        headers=upstream_headers,
        content=raw_body,
    )

    try:
        upstream_response = await model_client.send(req, stream=True)
    except httpx.RemoteProtocolError:
        upstream_response = await model_client.send(req, stream=True)

    resp_headers = {
        k: v for k, v in upstream_response.headers.multi_items()
        if k.lower() not in HOP_BY_HOP
    }

    if rewrite_kind == "anthropic":
        resp_headers = sanitize_rewritten_headers(resp_headers)

        async def generate():
            try:
                async for chunk in rewrite_anthropic_response(upstream_response, original_model):
                    yield chunk
            finally:
                await upstream_response.aclose()
    elif rewrite_kind == "openai-responses":
        resp_headers = sanitize_rewritten_headers(resp_headers)

        async def generate():
            try:
                async for chunk in rewrite_openai_response(upstream_response, original_model):
                    yield chunk
            finally:
                await upstream_response.aclose()
    else:
        async def generate():
            try:
                async for chunk in upstream_response.aiter_raw():
                    yield chunk
            finally:
                await upstream_response.aclose()

    return StreamingResponse(
        content=generate(),
        status_code=upstream_response.status_code,
        headers=resp_headers,
    )
