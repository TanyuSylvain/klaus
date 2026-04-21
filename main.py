import json
import logging
import httpx
import uvicorn
import yaml
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route

HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "transfer-encoding", "te", "trailers",
    "upgrade", "proxy-authorization", "proxy-authenticate", "content-length",
})

cfg: dict = {}
UPSTREAM_URL: str = ""
MAPPINGS: dict[str, str] = {}
HTTP_CLIENT: httpx.AsyncClient
LOGGER = logging.getLogger("llm_proxy")


async def proxy(request: Request) -> StreamingResponse:
    raw_body = await request.body()

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type and raw_body:
        try:
            data = json.loads(raw_body)
            if isinstance(data, dict) and "model" in data:
                original_model = data["model"]
                rewritten_model = MAPPINGS.get(original_model, original_model)
                LOGGER.info(
                    "model rewrite check path=%s inbound_model=%r outbound_model=%r",
                    request.url.path,
                    original_model,
                    rewritten_model,
                )
                data["model"] = rewritten_model
                raw_body = json.dumps(data).encode()
        except (json.JSONDecodeError, ValueError):
            pass

    upstream_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP
    }
    upstream_headers["host"] = httpx.URL(UPSTREAM_URL).host

    target = UPSTREAM_URL + request.url.path
    if request.url.query:
        target += "?" + request.url.query

    req = HTTP_CLIENT.build_request(
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


app = Starlette(routes=[
    Route("/{path:path}", proxy, methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"]),
])


def main():
    global cfg, UPSTREAM_URL, MAPPINGS, HTTP_CLIENT

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)

    UPSTREAM_URL = cfg["upstream_url"].rstrip("/")
    MAPPINGS = cfg.get("model_mappings", {})

    HTTP_CLIENT = httpx.AsyncClient(
        follow_redirects=False,
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=5.0),
    )

    uvicorn.run(
        app,
        host=cfg.get("listen_host", "0.0.0.0"),
        port=cfg.get("listen_port", 8080),
    )


if __name__ == "__main__":
    main()
