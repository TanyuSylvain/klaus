"""Shared utilities for the LLM proxy."""

from typing import Any

import httpx

HOP_BY_HOP = frozenset({
    "connection",
    "keep-alive",
    "transfer-encoding",
    "te",
    "trailers",
    "upgrade",
    "proxy-authorization",
    "proxy-authenticate",
    "content-length",
})

REWRITTEN_ENTITY_HEADERS = frozenset({
    "content-encoding",
    "content-length",
    "content-md5",
    "digest",
    "etag",
})


def build_client_kwargs(proxy_cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build httpx.AsyncClient kwargs from the optional proxy config section.

    Defaults match the original hardcoded values so existing configs keep the
    same behaviour.
    """
    proxy_cfg = proxy_cfg or {}

    return dict(
        http2=True,
        follow_redirects=False,
        limits=httpx.Limits(
            max_connections=int(proxy_cfg.get("max_connections", 200)),
            max_keepalive_connections=int(proxy_cfg.get("max_keepalive_connections", 50)),
            keepalive_expiry=float(proxy_cfg.get("keepalive_expiry", 5.0)),
        ),
        timeout=httpx.Timeout(
            connect=10.0,
            read=300.0,
            write=30.0,
            pool=float(proxy_cfg.get("pool_timeout", 5.0)),
        ),
    )
