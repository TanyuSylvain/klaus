"""Image proxy handler and SQLite usage logging."""

import json
import logging
import sqlite3
import time
from datetime import datetime

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from llm_proxy.utils import HOP_BY_HOP

LOGGER = logging.getLogger("llm_proxy")


def init_image_db(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS image_requests "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, "
            " api_key TEXT NOT NULL, "
            " timestamp TEXT NOT NULL, "
            " elapsed_ms REAL NOT NULL)"
        )


def log_image_request(db_path: str, api_key: str, elapsed_ms: float) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO image_requests (api_key, timestamp, elapsed_ms) VALUES (?, ?, ?)",
            (api_key, datetime.now().isoformat(), elapsed_ms),
        )


async def handle_image_request(
    request: Request,
    image_client: httpx.AsyncClient,
    upstream_url: str,
    api_key: str,
    allowed_models: set[str],
    db_path: str,
) -> StreamingResponse:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        caller_key = auth[7:]
    else:
        caller_key = ""
    if not caller_key:
        return JSONResponse(
            {"error": {"message": "Missing Authorization header"}},
            status_code=401,
        )

    raw_body = await request.body()

    model = ""
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type and raw_body:
        try:
            data = json.loads(raw_body)
            if isinstance(data, dict):
                model = data.get("model", "")
        except (json.JSONDecodeError, ValueError):
            pass
    elif "multipart/form-data" in content_type:
        form = await request.form()
        model = form.get("model", "")
        if not isinstance(model, str):
            model = ""

    if model and model not in allowed_models:
        return JSONResponse(
            {"error": {"message": f"Model '{model}' is not allowed"}},
            status_code=403,
        )

    upstream_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP
    }
    upstream_headers["host"] = httpx.URL(upstream_url).host
    upstream_headers["authorization"] = f"Bearer {api_key}"

    target = upstream_url + request.url.path
    if request.url.query:
        target += "?" + request.url.query

    LOGGER.info("image proxy path=%s model=%s target=%s", request.url.path, model, target)

    t0 = time.monotonic()
    req = httpx.Request(
        method=request.method,
        url=target,
        headers=upstream_headers,
        content=raw_body,
    )
    try:
        upstream_response = await image_client.send(req, stream=True)
    except httpx.RemoteProtocolError:
        upstream_response = await image_client.send(req, stream=True)
    elapsed_ms = (time.monotonic() - t0) * 1000

    if upstream_response.status_code < 400:
        log_image_request(db_path, caller_key, elapsed_ms)

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
