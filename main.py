"""LLM proxy entry point.

Loads config.yaml, wires the LlmProxyFeature and optional CchStatusFeature,
and starts uvicorn.
"""

import logging
import sys
from typing import Any

import uvicorn
import yaml
from starlette.applications import Starlette

from cch_manage import CchStatusConfig, CchStatusFeature
from llm_proxy import LlmProxyConfig, LlmProxyFeature
from llm_proxy.utils import build_client_kwargs

LOGGER = logging.getLogger("llm_proxy")


def build_routes(
    llm_proxy_feature: LlmProxyFeature,
    cch_status_feature: CchStatusFeature | None = None,
) -> list:
    routes = list(llm_proxy_feature.routes())
    if cch_status_feature is not None:
        # The LLM proxy registers a catch-all /{path:path} route as its last
        # route. CCH public status routes must be matched before it, otherwise
        # unauthenticated status page requests are rejected with 401.
        catch_all_index = next(
            (
                i
                for i, route in enumerate(routes)
                if getattr(route, "path", None) == "/{path:path}"
            ),
            None,
        )
        if catch_all_index is not None:
            catch_all = routes.pop(catch_all_index)
            routes.extend(cch_status_feature.routes())
            routes.append(catch_all)
        else:
            routes.extend(cch_status_feature.routes())
    return routes


def make_lifespan(
    client_kwargs: dict[str, Any],
    llm_proxy_feature: LlmProxyFeature,
    cch_status_feature: CchStatusFeature | None = None,
):
    async def lifespan(_: Starlette):
        await llm_proxy_feature.start(client_kwargs)
        if cch_status_feature is not None:
            await cch_status_feature.start(client_kwargs)
            await cch_status_feature.warmup()
        try:
            yield
        finally:
            await llm_proxy_feature.aclose()
            if cch_status_feature is not None:
                await cch_status_feature.aclose()
    return lifespan


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)

    try:
        llm_proxy_config = LlmProxyConfig.from_dict(cfg)
    except ValueError as exc:
        LOGGER.error("Invalid llm_proxy configuration: %s", exc)
        sys.exit(1)

    llm_proxy_feature = LlmProxyFeature(llm_proxy_config)
    LOGGER.info(
        "Model proxy configured with %d provider(s): %s",
        len(llm_proxy_config.providers),
        ", ".join(llm_proxy_config.providers),
    )
    if llm_proxy_config.mcp is not None:
        LOGGER.info("MCP proxy enabled, upstream=%s", llm_proxy_config.mcp.upstream_url)
    else:
        LOGGER.info("MCP proxy disabled (no mcp config section)")
    if llm_proxy_config.image is not None:
        LOGGER.info(
            "Image proxy enabled, upstream=%s, models=%s",
            llm_proxy_config.image.upstream_url,
            llm_proxy_config.image.allowed_models,
        )
    else:
        LOGGER.info("Image proxy disabled (no image config section)")

    cch_status_config = CchStatusConfig.from_dict(cfg.get("cch_status"))
    cch_status_feature = CchStatusFeature(cch_status_config) if cch_status_config is not None else None
    if cch_status_feature is not None:
        quota_labels = [
            (
                f"{group.status_slug}->{group.provider_group}"
                if group.status_slug != group.provider_group
                else group.status_slug
            )
            for group in cch_status_config.quota_groups
        ]
        LOGGER.info(
            "CCH status enabled, upstream=%s, quota_groups=%s",
            cch_status_config.base_url,
            ", ".join(quota_labels) if quota_labels else "(none)",
        )
    else:
        LOGGER.info("CCH status disabled (no enabled cch_status config section)")

    app = Starlette(
        routes=build_routes(llm_proxy_feature, cch_status_feature),
        lifespan=make_lifespan(
            build_client_kwargs(cfg.get("proxy")),
            llm_proxy_feature,
            cch_status_feature,
        ),
    )

    uvicorn.run(
        app,
        host=cfg.get("listen_host", "0.0.0.0"),
        port=cfg.get("listen_port", 8080),
        timeout_graceful_shutdown=300,
    )


if __name__ == "__main__":
    main()
