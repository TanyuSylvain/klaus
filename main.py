import logging
import re
import httpx
import uvicorn
import yaml
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.responses import StreamingResponse
from starlette.routing import Mount, Route

HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "transfer-encoding", "te", "trailers",
    "upgrade", "proxy-authorization", "proxy-authenticate", "content-length",
})

cfg: dict = {}
UPSTREAM_URL: str = ""
MAPPINGS: dict[str, str] = {}
HTTP_CLIENT: httpx.AsyncClient
LOGGER = logging.getLogger("llm_proxy")

MCP_UPSTREAM_URL: str = ""
MCP_API_KEY: str = ""
MCP_ENABLED: bool = False


def models_payload() -> dict:
    return {
        "data": [
            {
                "id": model_name,
                "type": "model",
                "display_name": model_name,
                "created_at": "2025-01-01T00:00:00Z",
            }
            for model_name in MAPPINGS
        ],
        "has_more": False,
    }


async def list_models(_: Request) -> JSONResponse:
    return JSONResponse(models_payload())


async def proxy(request: Request) -> StreamingResponse:
    raw_body = await request.body()

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type and raw_body:
        match = re.search(rb'"model"\s*:\s*"([^"]+)"', raw_body)
        if match:
            original_model = match.group(1).decode()
            rewritten_model = MAPPINGS.get(original_model, original_model)
            LOGGER.info(
                "model rewrite check path=%s inbound_model=%r outbound_model=%r",
                request.url.path,
                original_model,
                rewritten_model,
            )
            if original_model != rewritten_model:
                raw_body = re.sub(
                    rb'("model"\s*:\s*")([^"]+)(")',
                    lambda m: m.group(1) + rewritten_model.encode() + m.group(3),
                    raw_body,
                    count=1,
                )

    upstream_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP
    }
    upstream_headers["host"] = httpx.URL(UPSTREAM_URL).host

    target = UPSTREAM_URL + request.url.path
    if request.url.query:
        target += "?" + request.url.query

    req = httpx.Request(
        method=request.method,
        url=target,
        headers=upstream_headers,
        content=raw_body,
    )
    try:
        upstream_response = await HTTP_CLIENT.send(req, stream=True)
    except httpx.RemoteProtocolError:
        # Stale keepalive connection — retry once with a fresh connection.
        upstream_response = await HTTP_CLIENT.send(req, stream=True)

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


async def mcp_proxy(request: Request) -> StreamingResponse:
    raw_body = await request.body()

    upstream_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP
    }
    upstream_headers["host"] = httpx.URL(MCP_UPSTREAM_URL).host
    upstream_headers["authorization"] = f"Bearer {MCP_API_KEY}"

    # Strip the /mcp prefix that Mount matched
    path = request.url.path
    if path.startswith("/mcp"):
        path = path[4:] or "/"

    target = MCP_UPSTREAM_URL + path
    if request.url.query:
        target += "?" + request.url.query

    LOGGER.info("mcp proxy path=%s target=%s", path, target)

    req = HTTP_CLIENT.build_request(
        method=request.method,
        url=target,
        headers=upstream_headers,
        content=raw_body,
    )
    try:
        upstream_response = await HTTP_CLIENT.send(req, stream=True)
    except httpx.RemoteProtocolError:
        upstream_response = await HTTP_CLIENT.send(req, stream=True)

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


def build_routes() -> list:
    routes = [
        Route("/models", list_models, methods=["GET"]),
        Route("/v1/models", list_models, methods=["GET"]),
    ]
    if MCP_ENABLED:
        routes.append(
            Mount("/mcp", routes=[
                Route("/{path:path}", mcp_proxy, methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"]),
            ])
        )
    routes.append(
        Route("/{path:path}", proxy, methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"]),
    )
    return routes


def main():
    global cfg, UPSTREAM_URL, MAPPINGS, HTTP_CLIENT, MCP_UPSTREAM_URL, MCP_API_KEY, MCP_ENABLED, app

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)

    UPSTREAM_URL = cfg["model_mappings"]["upstream_url"].rstrip("/")
    MAPPINGS = cfg["model_mappings"].get("models", {})

    HTTP_CLIENT = httpx.AsyncClient(
        http2=True,
        follow_redirects=False,
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=5.0),
    )

    mcp_cfg = cfg.get("mcp")
    if mcp_cfg and mcp_cfg.get("upstream_url") and mcp_cfg.get("api_key"):
        MCP_UPSTREAM_URL = mcp_cfg["upstream_url"].rstrip("/")
        MCP_API_KEY = mcp_cfg["api_key"]
        MCP_ENABLED = True
        LOGGER.info("MCP proxy enabled, upstream=%s", MCP_UPSTREAM_URL)
    else:
        LOGGER.info("MCP proxy disabled (no mcp config section)")

    app = Starlette(routes=build_routes())

    uvicorn.run(
        app,
        host=cfg.get("listen_host", "0.0.0.0"),
        port=cfg.get("listen_port", 8080),
    )


if __name__ == "__main__":
    main()
