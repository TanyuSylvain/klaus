"""LlmProxyFeature: wires routes, lifecycle, and handler entrypoints."""

from typing import Any

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import BaseRoute, Mount, Route

from llm_proxy.config import LlmProxyConfig, Provider
from llm_proxy.image import handle_image_request, init_image_db
from llm_proxy.mcp import handle_mcp_request
from llm_proxy.proxy import handle_list_models, handle_proxy_request, resolve_provider


class LlmProxyFeature:
    def __init__(self, config: LlmProxyConfig):
        self.config = config
        self.providers: dict[str, Provider] = config.providers
        self.model_client: httpx.AsyncClient | None = None
        self.mcp_client: httpx.AsyncClient | None = None
        self.image_client: httpx.AsyncClient | None = None

    def routes(self) -> list[BaseRoute]:
        routes: list[BaseRoute] = [
            Route("/models", self.list_models, methods=["GET"]),
            Route("/v1/models", self.list_models, methods=["GET"]),
        ]
        if self.config.mcp is not None:
            routes.append(
                Mount(
                    "/mcp",
                    routes=[
                        Route(
                            "/{path:path}",
                            self.mcp_proxy,
                            methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"],
                        ),
                    ],
                )
            )
        if self.config.image is not None:
            routes.append(Route("/v1/images/generations", self.image_proxy, methods=["POST"]))
            routes.append(Route("/v1/images/edits", self.image_proxy, methods=["POST"]))
        routes.append(
            Route(
                "/{path:path}",
                self.proxy,
                methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"],
            )
        )
        return routes

    async def start(self, client_kwargs: dict[str, Any]) -> None:
        self.model_client = httpx.AsyncClient(**client_kwargs)
        if self.config.mcp is not None:
            self.mcp_client = httpx.AsyncClient(**client_kwargs)
        if self.config.image is not None:
            self.image_client = httpx.AsyncClient(**client_kwargs)
            init_image_db(self.config.image.db_path)

    async def aclose(self) -> None:
        if self.model_client is not None:
            await self.model_client.aclose()
        if self.mcp_client is not None:
            await self.mcp_client.aclose()
        if self.image_client is not None:
            await self.image_client.aclose()

    async def list_models(self, request: Request) -> JSONResponse:
        return await handle_list_models(request, self.providers)

    async def proxy(self, request: Request) -> StreamingResponse:
        provider = resolve_provider(request, self.providers)
        if provider is None:
            return JSONResponse(
                {"error": {"message": "Invalid API key"}},
                status_code=401,
            )
        if self.model_client is None:
            raise RuntimeError("LlmProxyFeature has not been started")
        return await handle_proxy_request(request, provider, self.model_client)

    async def mcp_proxy(self, request: Request) -> StreamingResponse:
        if self.config.mcp is None or self.mcp_client is None:
            raise RuntimeError("MCP proxy is not configured or started")
        return await handle_mcp_request(
            request,
            self.mcp_client,
            self.config.mcp.upstream_url,
            self.config.mcp.api_key,
        )

    async def image_proxy(self, request: Request) -> StreamingResponse:
        if self.config.image is None or self.image_client is None:
            raise RuntimeError("Image proxy is not configured or started")
        return await handle_image_request(
            request,
            self.image_client,
            self.config.image.upstream_url,
            self.config.image.api_key,
            self.config.image.allowed_models,
            self.config.image.db_path,
        )
