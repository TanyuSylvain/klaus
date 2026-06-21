import logging
import time
from asyncio import Lock
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from cch_manage import client as cch_client
from cch_manage.config import CchStatusConfig
from cch_manage.leaderboard import (
    active_leaderboard_entries,
    api_key_hash,
    get_weekly_leaderboard as build_weekly_leaderboard,
    public_leaderboard_entry,
    weekly_total_requests,
    weekly_total_used_usd,
)
from cch_manage.price_coefficients import extract_group_price_coefficients
from cch_manage.quota import manage_provider_group_quota
from cch_manage.status_page import inject_status_script, rewrite_root_relative_urls
from cch_manage.utils import (
    decimal_from_any,
    money_float,
    percent_float,
    public_status_ttl_seconds,
    seconds_until,
)


LOGGER = logging.getLogger("llm_proxy.cch_status")


@dataclass(frozen=True)
class _PublicStatusCacheEntry:
    payload: dict[str, Any]
    status_code: int
    expires_at: float


class CchStatusFeature:
    def __init__(self, config: CchStatusConfig):
        self.config = config
        self.client: httpx.AsyncClient | None = None
        self._cache: dict[str, Any] | None = None
        self._cache_expires_at = 0.0
        self._leaderboard_cache: dict[str, Any] | None = None
        self._leaderboard_cache_expires_at = 0.0
        self._price_coefficients_cache: dict[str, Any] | None = None
        self._price_coefficients_cache_expires_at = 0.0
        self._public_status_cache: dict[str, _PublicStatusCacheEntry] = {}
        self._public_status_lock = Lock()
        self._auto_disabled_provider_ids: set[int] = set()

    def routes(self) -> list[Route]:
        return [
            Route("/api/public-status", self.public_status, methods=["GET"]),
            Route("/api/public-site-meta", self.public_site_meta, methods=["GET"]),
            Route("/_next/{path:path}", self.next_asset, methods=["GET"]),
            Route(f"/{self.config.locale}/status", self.status_page, methods=["GET"]),
            Route(f"/{self.config.locale}/status/{{group_slug}}", self.status_page, methods=["GET"]),
            Route("/cch/status/weekly-consumption", self.weekly_consumption, methods=["GET"]),
            Route("/cch/status/weekly-leaderboard", self.weekly_leaderboard, methods=["GET"]),
            Route("/cch/status/weekly-user-consumption", self.weekly_user_consumption, methods=["GET"]),
            Route("/cch/status/group-price-coefficients", self.group_price_coefficients, methods=["GET"]),
        ]

    async def start(self, client_kwargs: dict[str, Any]) -> None:
        self.client = httpx.AsyncClient(**client_kwargs)

    async def warmup(self) -> None:
        if not self.config.weekly_leaderboard_enabled:
            LOGGER.info("skipping CCH weekly user usage cache warm-up; leaderboard is disabled")
            return

        started_at = time.monotonic()
        try:
            payload = await self.get_weekly_leaderboard()
        except Exception:
            LOGGER.exception("failed to warm CCH weekly user usage cache")
            return

        user_usage = payload.get("userUsage")
        user_count = len(user_usage) if isinstance(user_usage, list) else 0
        LOGGER.info(
            "warmed CCH weekly user usage cache users=%s elapsed_ms=%.1f",
            user_count,
            (time.monotonic() - started_at) * 1000,
        )

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()

    async def weekly_consumption(self, _: Request) -> JSONResponse:
        try:
            payload = await self.get_weekly_consumption()
        except Exception as exc:
            LOGGER.exception("failed to build CCH weekly consumption")
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=502,
            )
        return JSONResponse({"ok": True, "data": payload})

    async def weekly_leaderboard(self, _: Request) -> JSONResponse:
        try:
            payload = self._public_weekly_leaderboard_payload(
                await self.get_weekly_leaderboard()
            )
        except Exception as exc:
            LOGGER.exception("failed to build CCH weekly leaderboard")
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=502,
            )
        return JSONResponse({"ok": True, "data": payload})

    async def weekly_user_consumption(self, request: Request) -> JSONResponse:
        api_key = (
            request.query_params.get("apiKey")
            or request.query_params.get("api_key")
            or ""
        ).strip()
        if not api_key:
            return JSONResponse(
                {"ok": False, "error": "apiKey query parameter is required"},
                status_code=400,
            )

        payload = self.get_cached_weekly_user_consumption(api_key)
        if payload is None:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "weekly user usage is not available in cache",
                },
                status_code=404,
            )
        return JSONResponse({"ok": True, "data": payload})

    async def group_price_coefficients(self, _: Request) -> JSONResponse:
        try:
            payload = await self.get_group_price_coefficients()
        except Exception as exc:
            LOGGER.exception("failed to build CCH group price coefficients")
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=502,
            )
        return JSONResponse({"ok": True, "data": payload})

    async def public_status(self, request: Request) -> JSONResponse:
        try:
            payload, status_code, expires_at = await self._get_cached_public_status(request.url.query)
        except Exception as exc:
            LOGGER.exception("failed to proxy CCH public status")
            return JSONResponse(
                {"error": "CCH public status unavailable", "detail": str(exc)},
                status_code=502,
                headers={
                    "cache-control": "no-store",
                    "x-llm-proxy-cch-route": "public-status",
                },
            )
        return JSONResponse(
            payload,
            status_code=status_code,
            headers={
                "cache-control": "no-store",
                "x-llm-proxy-cch-route": "public-status",
                "x-llm-proxy-cch-cache-expires-in-seconds": seconds_until(expires_at),
            },
        )

    async def public_site_meta(self, request: Request) -> Response:
        client = self._require_client()
        upstream = await client.get(
            f"{self.config.base_url}/api/public-site-meta",
            params=list(request.query_params.multi_items()),
        )
        return Response(
            upstream.content,
            status_code=upstream.status_code,
            media_type="application/json",
            headers={
                "cache-control": "no-store",
                "x-llm-proxy-cch-route": "public-site-meta",
            },
        )

    async def next_asset(self, request: Request) -> Response:
        client = self._require_client()
        path = request.path_params.get("path", "")
        target = f"{self.config.base_url}/_next/{path}"
        upstream = await client.get(
            target,
            params=list(request.query_params.multi_items()),
        )
        headers = {
            key: value
            for key, value in upstream.headers.multi_items()
            if key.lower() in {"cache-control", "etag", "last-modified"}
        }
        return Response(
            upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type") or None,
            headers=headers,
        )

    async def status_page(self, request: Request) -> Response:
        client = self._require_client()
        group_slug = request.path_params.get("group_slug")
        path = f"/{self.config.locale}/status"
        if group_slug:
            path = f"{path}/{group_slug}"
        target = f"{self.config.base_url}{path}"
        upstream = await client.get(target)

        content_type = upstream.headers.get("content-type", "")
        if upstream.status_code >= 400 or "text/html" not in content_type:
            return Response(
                upstream.content,
                status_code=upstream.status_code,
                media_type=content_type or None,
            )

        html = rewrite_root_relative_urls(
            upstream.text,
            base_url=self.config.base_url,
            locale=self.config.locale,
        )
        return HTMLResponse(
            inject_status_script(
                html,
                self.config.status_group_slug,
                self.config.cache_ttl_seconds,
            ),
            headers={"cache-control": "no-store"},
        )

    async def get_weekly_consumption(self) -> dict[str, Any]:
        now_monotonic = time.monotonic()
        if self._cache is not None and now_monotonic < self._cache_expires_at:
            return self._cache

        week_start, week_end = self._current_week_range()
        _, providers = await cch_client.get_claude_group_providers(
            self._post_action,
            self.config.provider_group,
        )
        provider_ids = [int(provider["id"]) for provider in providers if provider.get("id") is not None]

        used = decimal_from_any(0)
        for provider_id in provider_ids:
            provider_used = await cch_client.get_provider_weekly_usage(self._post_action, provider_id)
            used += provider_used

        quota = self.config.weekly_quota_usd
        remaining = quota - used if quota is not None else None
        usage_pct = (used / quota * decimal_from_any(100)) if quota and quota > 0 else None
        auto_manage = await self._manage_provider_group_quota(providers, usage_pct)

        payload = {
            "weekStart": week_start.isoformat(),
            "weekEnd": week_end.isoformat(),
            "providerGroup": self.config.provider_group,
            "statusGroupSlug": self.config.status_group_slug,
            "usedUsd": money_float(used),
            "quotaUsd": money_float(quota),
            "remainingUsd": money_float(remaining),
            "usagePct": percent_float(usage_pct),
            "providerIds": provider_ids,
            "source": "providerLimitUsage",
            "autoManageProviderGroupQuota": auto_manage,
            "weeklyLeaderboardEnabled": self.config.weekly_leaderboard_enabled,
            "generatedAt": datetime.now(ZoneInfo(self.config.timezone)).isoformat(),
        }

        self._cache = payload
        self._cache_expires_at = now_monotonic + max(1, self.config.cache_ttl_seconds)
        return payload

    async def get_weekly_leaderboard(self) -> dict[str, Any]:
        now_monotonic = time.monotonic()
        if (
            self._leaderboard_cache is not None
            and now_monotonic < self._leaderboard_cache_expires_at
        ):
            return self._leaderboard_cache

        week_start, week_end = self._current_week_range()
        leaderboard: list[dict[str, Any]] = []
        if self.config.weekly_leaderboard_enabled:
            leaderboard = await build_weekly_leaderboard(
                self._post_action,
                week_start,
                week_end,
                ZoneInfo(self.config.timezone),
            )

        payload = {
            "weekStart": week_start.isoformat(),
            "weekEnd": week_end.isoformat(),
            "weeklyLeaderboardEnabled": self.config.weekly_leaderboard_enabled,
            "leaderboard": [
                public_leaderboard_entry(entry)
                for entry in active_leaderboard_entries(leaderboard)[:3]
            ],
            "weeklyTotalRequests": weekly_total_requests(leaderboard),
            "weeklyTotalUsedUsd": weekly_total_used_usd(leaderboard),
            "userUsage": leaderboard,
            "generatedAt": datetime.now(ZoneInfo(self.config.timezone)).isoformat(),
        }
        self._leaderboard_cache = payload
        self._leaderboard_cache_expires_at = (
            now_monotonic + max(1, self.config.cache_ttl_seconds)
        )
        return payload

    def _public_weekly_leaderboard_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in payload.items()
            if key != "userUsage"
        }

    def get_cached_weekly_user_consumption(self, api_key: str) -> dict[str, Any] | None:
        if self._leaderboard_cache is None:
            return None

        user_usage = self._leaderboard_cache.get("userUsage", [])
        total_user = len(user_usage) if isinstance(user_usage, list) else 0
        target_hash = api_key_hash(api_key)
        for entry in user_usage:
            if not isinstance(entry, dict):
                continue
            key_hashes = entry.get("apiKeyHashes")
            if not isinstance(key_hashes, list) or target_hash not in key_hashes:
                continue

            now_monotonic = time.monotonic()
            cache_expires_at = self._leaderboard_cache_expires_at
            return {
                "weekStart": self._leaderboard_cache.get("weekStart"),
                "weekEnd": self._leaderboard_cache.get("weekEnd"),
                "userId": entry.get("userId"),
                "userName": entry.get("name"),
                "usedUsd": entry.get("usedUsd"),
                "requestCount": entry.get("requestCount"),
                "rank": entry.get("rank"),
                "totalUser": total_user,
                "generatedAt": self._leaderboard_cache.get("generatedAt"),
                "cacheExpiresInSeconds": max(0, round(cache_expires_at - now_monotonic, 3)),
                "stale": now_monotonic >= cache_expires_at,
                "source": "weeklyLeaderboardCache",
            }

        return None

    async def get_group_price_coefficients(self) -> dict[str, Any]:
        now_monotonic = time.monotonic()
        if (
            self._price_coefficients_cache is not None
            and now_monotonic < self._price_coefficients_cache_expires_at
        ):
            return self._price_coefficients_cache

        providers, _ = await cch_client.get_claude_group_providers(
            self._post_action,
            self.config.provider_group,
        )
        public_status = await cch_client.get_public_status_groups(self._get_public_status_payload)
        groups = extract_group_price_coefficients(
            providers,
            public_status,
            self.config.status_group_provider_groups,
        )
        payload = {
            "groups": groups,
            "generatedAt": datetime.now(ZoneInfo(self.config.timezone)).isoformat(),
        }
        self._price_coefficients_cache = payload
        self._price_coefficients_cache_expires_at = (
            now_monotonic + max(1, self.config.cache_ttl_seconds)
        )
        return payload

    async def _get_cached_public_status(self, query: str) -> tuple[dict[str, Any], int, float]:
        now_monotonic = time.monotonic()
        cached = self._public_status_cache.get(query)
        if cached is not None and now_monotonic < cached.expires_at:
            return cached.payload, cached.status_code, cached.expires_at

        async with self._public_status_lock:
            now_monotonic = time.monotonic()
            cached = self._public_status_cache.get(query)
            if cached is not None and now_monotonic < cached.expires_at:
                return cached.payload, cached.status_code, cached.expires_at

            try:
                payload, status_code = await self._fetch_public_status(query)
            except Exception:
                if cached is not None:
                    LOGGER.exception(
                        "failed to refresh CCH public status; serving stale cache",
                        extra={"query": query},
                    )
                    return cached.payload, cached.status_code, cached.expires_at
                raise

            expires_at = now_monotonic
            if status_code in (200, 503):
                ttl_seconds = public_status_ttl_seconds(
                    payload,
                    now=datetime.now(UTC),
                    fallback_seconds=max(1, self.config.cache_ttl_seconds),
                )
                expires_at = now_monotonic + ttl_seconds
                self._public_status_cache[query] = _PublicStatusCacheEntry(
                    payload=payload,
                    status_code=status_code,
                    expires_at=expires_at,
                )
            return payload, status_code, expires_at

    async def _get_public_status_payload(self, query: str) -> dict[str, Any]:
        payload, status_code, _ = await self._get_cached_public_status(query)
        if status_code >= 400:
            raise RuntimeError(f"CCH public status failed with HTTP {status_code}")
        return payload

    async def _fetch_public_status(self, query: str) -> tuple[dict[str, Any], int]:
        return await cch_client.fetch_public_status(
            self._require_client(),
            self.config.base_url,
            query,
        )

    def _current_week_range(self) -> tuple[datetime, datetime]:
        tz = ZoneInfo(self.config.timezone)
        now = datetime.now(tz)
        week_start = (now - timedelta(days=now.weekday())).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        return week_start, week_start + timedelta(days=7)

    async def _manage_provider_group_quota(
        self,
        providers: list[dict[str, Any]],
        usage_pct: Decimal | None,
    ) -> dict[str, Any]:
        return await manage_provider_group_quota(
            providers,
            usage_pct,
            enabled=self.config.auto_manage_provider_group_quota,
            weekly_quota_usd=self.config.weekly_quota_usd,
            threshold_pct=self.config.auto_disable_threshold_pct,
            auto_disabled_provider_ids=self._auto_disabled_provider_ids,
            set_provider_enabled=self._set_provider_enabled,
        )

    async def _set_provider_enabled(self, provider_id: int, enabled: bool) -> bool:
        try:
            await cch_client.set_provider_enabled(self._post_action, provider_id, enabled)
        except Exception:
            LOGGER.exception(
                "failed to set CCH provider enabled state",
                extra={"provider_id": provider_id, "enabled": enabled},
            )
            return False
        return True

    async def _post_action(self, path: str, payload: dict[str, Any]) -> Any:
        return await cch_client.post_action(
            self._require_client(),
            self.config.base_url,
            self.config.admin_token,
            path,
            payload,
        )

    def _require_client(self) -> httpx.AsyncClient:
        if self.client is None:
            raise RuntimeError("CCH status client is not started")
        return self.client
