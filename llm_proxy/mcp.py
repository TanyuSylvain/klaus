"""MCP proxy handler."""

import logging

import httpx
from starlette.requests import Request
from starlette.responses import StreamingResponse

from llm_proxy.utils import HOP_BY_HOP

LOGGER = logging.getLogger("llm_proxy")


async def handle_mcp_request(
    request: Request,
    mcp_client: httpx.AsyncClient,
    upstream_url: str,
    api_key: str,
) -> StreamingResponse:
    raw_body = await request.body()

    upstream_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP
    }
    upstream_headers["host"] = httpx.URL(upstream_url).host
    upstream_headers["authorization"] = f"Bearer {api_key}"

    # Strip the /mcp prefix that Mount matched
    path = request.url.path
    if path.startswith("/mcp"):
        path = path[4:] or "/"

    target = upstream_url + path
    if request.url.query:
        target += "?" + request.url.query

    LOGGER.info("mcp proxy path=%s target=%s", path, target)

    req = mcp_client.build_request(
        method=request.method,
        url=target,
        headers=upstream_headers,
        content=raw_body,
    )
    try:
        upstream_response = await mcp_client.send(req, stream=True)
    except httpx.RemoteProtocolError:
        upstream_response = await mcp_client.send(req, stream=True)

    resp_headers = {
        k: v for k, v in upstream_response.headers.multi_items()
        if k.lower() not in HOP_BY_HOP
    }

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
