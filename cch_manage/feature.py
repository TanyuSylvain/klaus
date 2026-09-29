import asyncio
import logging
import time
from asyncio import Lock
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from cch_manage.client import CchClient
from cch_manage.config import CchStatusConfig
from cch_manage.leaderboard import (
    active_leaderboard_entries,
    api_key_hash,
    get_weekly_leaderboard as build_weekly_leaderboard,
    public_leaderboard_entry,
    weekly_total_requests,
    weekly_total_used_usd,
)
from cch_manage.group_usage import (
    build_group_usage_payload,
    collect_provider_usage_stats,
    provider_ids_by_status_slug,
)
from cch_manage.price_coefficients import extract_group_price_coefficients
from cch_manage.provider_usage import ProviderUsageManager
from cch_manage.quota import manage_provider_group_quota, quota_usage_summary
from cch_manage.status_page import inject_status_script, rewrite_root_relative_urls
from cch_manage.utils import (
    money_float,
    percent_float,
    provider_groups,
    public_status_ttl_seconds,
    seconds_until,
)


LOGGER = logging.getLogger("llm_proxy.cch_status")


def _provider_ids(providers: list[dict[str, Any]]) -> list[int]:
    ids: list[int] = []
    for provider in providers:
        provider_id = provider.get("id")
        if isinstance(provider_id, int) and not isinstance(provider_id, bool):
            ids.append(provider_id)
    return ids


@dataclass(frozen=True)
class _PublicStatusCacheEntry:
    payload: dict[str, Any]
    status_code: int
    expires_at: float


class CchStatusFeature:
    def __init__(self, config: CchStatusConfig):
        self.config = config
        self.client: httpx.AsyncClient | None = None
        self._cch: CchClient | None = None
        self._cache: dict[str, Any] | None = None
        self._cache_expires_at = 0.0
        self._leaderboard_cache: dict[str, Any] | None = None
        self._leaderboard_cache_expires_at = 0.0
        self._price_coefficients_cache: dict[str, Any] | None = None
        self._price_coefficients_cache_expires_at = 0.0
        self._group_usage_cache: dict[str, Any] | None = None
        self._group_usage_cache_expires_at = 0.0
        self._public_status_cache: dict[str, _PublicStatusCacheEntry] = {}
        self._public_status_lock = Lock()
        self._auto_disabled_provider_ids: dict[str, set[int]] = {
            group.provider_group: set() for group in config.quota_groups
        }
        self._usage_manager: ProviderUsageManager | None = None
        self._usage_task: asyncio.Task[None] | None = None
        if config.usage_monitors:
            self._usage_manager = ProviderUsageManager(
                config.usage_monitors,
                config.usage_query,
                status_group_provider_groups=config.effective_card_provider_groups(),
            )

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
            Route("/cch/status/group-usage", self.group_usage, methods=["GET"]),
            Route("/cch/status/provider-usage", self.provider_usage, methods=["GET"]),
        ]

    async def start(self, client_kwargs: dict[str, Any]) -> None:
        self.client = httpx.AsyncClient(**client_kwargs)
        self._cch = CchClient(self.client, self.config.base_url, self.config.admin_token)
        if self._usage_manager is not None:
            self._usage_task = asyncio.create_task(
                self._usage_monitor_loop(),
                name="cch-provider-usage-monitor",
            )
            LOGGER.info(
                "CCH provider usage monitors started count=%s",
                len(self.config.usage_monitors),
            )

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
        if self._usage_task is not None:
            self._usage_task.cancel()
            try:
                await self._usage_task
            except asyncio.CancelledError:
                pass
            self._usage_task = None
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

    async def group_usage(self, _: Request) -> JSONResponse:
        try:
            payload = await self.get_group_usage()
        except Exception as exc:
            LOGGER.exception("failed to build CCH group usage")
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=502,
            )
        return JSONResponse({"ok": True, "data": payload})

    async def provider_usage(self, _: Request) -> JSONResponse:
        if self._usage_manager is None:
            return JSONResponse(
                {
                    "ok": True,
                    "data": {"groups": []},
                }
            )
        return JSONResponse({"ok": True, "data": self._usage_manager.public_payload()})

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
                [group.status_slug for group in self.config.quota_groups],
                self.config.cache_ttl_seconds,
            ),
            headers={"cache-control": "no-store"},
        )

    async def get_weekly_consumption(self) -> dict[str, Any]:
        now_monotonic = time.monotonic()
        if self._cache is not None and now_monotonic < self._cache_expires_at:
            return self._cache

        week_start, week_end = self._current_week_range()
        groups: list[dict[str, Any]] = []
        if self.config.quota_groups:
            groups = await self._build_quota_group_payloads()

        payload = {
            "weekStart": week_start.isoformat(),
            "weekEnd": week_end.isoformat(),
            "groups": groups,
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
                self._require_cch(),
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

        providers = await self._require_cch().list_providers()
        public_status = await self._get_public_status_payload("include=groups")
        raw_groups = public_status.get("groups")
        public_status_groups = (
            [group for group in raw_groups if isinstance(group, dict)]
            if isinstance(raw_groups, list)
            else []
        )
        groups = extract_group_price_coefficients(
            providers,
            public_status_groups,
            self.config.effective_card_provider_groups(),
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

    async def get_group_usage(self) -> dict[str, Any]:
        now_monotonic = time.monotonic()
        if (
            self._group_usage_cache is not None
            and now_monotonic < self._group_usage_cache_expires_at
        ):
            return self._group_usage_cache

        week_start, week_end = self._current_week_range()
        tz = week_start.tzinfo
        now = datetime.now(tz)
        providers = await self._require_cch().list_providers()
        public_status = await self._get_public_status_payload("include=groups")
        raw_groups = public_status.get("groups")
        public_status_groups = (
            [group for group in raw_groups if isinstance(group, dict)]
            if isinstance(raw_groups, list)
            else []
        )
        ids_by_slug = provider_ids_by_status_slug(
            providers,
            public_status_groups,
            self.config.effective_card_provider_groups(),
        )
        provider_ids = sorted({
            provider_id
            for ids in ids_by_slug.values()
            for provider_id in ids
        })
        stats_by_provider = await collect_provider_usage_stats(
            self._require_cch(),
            provider_ids,
            int(week_start.timestamp() * 1000),
            int(now.timestamp() * 1000),
        )
        payload = build_group_usage_payload(
            ids_by_slug,
            stats_by_provider,
            week_start,
            week_end,
            datetime.now(ZoneInfo(self.config.timezone)),
        )
        self._group_usage_cache = payload
        self._group_usage_cache_expires_at = (
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
        return await self._require_cch().get_public_status(query)

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

    async def _build_quota_group_payloads(self) -> list[dict[str, Any]]:
        providers = await self._require_cch().list_providers()
        providers_by_group: dict[str, list[dict[str, Any]]] = {}
        provider_ids_by_group: dict[str, list[int]] = {}
        all_ids: list[int] = []
        seen_ids: set[int] = set()
        for group in self.config.quota_groups:
            matched = [
                provider
                for provider in providers
                if group.provider_group in provider_groups(provider)
            ]
            providers_by_group[group.provider_group] = matched
            ids = _provider_ids(matched)
            provider_ids_by_group[group.provider_group] = ids
            for provider_id in ids:
                if provider_id not in seen_ids:
                    seen_ids.add(provider_id)
                    all_ids.append(provider_id)

        usages: dict[int, dict[str, Any]] = {}
        if all_ids:
            usages = await self._require_cch().get_providers_limit_usage(all_ids)

        payloads: list[dict[str, Any]] = []
        for group in self.config.quota_groups:
            provider_ids = provider_ids_by_group[group.provider_group]
            summary = quota_usage_summary(provider_ids, usages, group.weekly_quota_usd)
            disabled_ids = self._auto_disabled_provider_ids.setdefault(
                group.provider_group,
                set(),
            )
            auto_manage = await manage_provider_group_quota(
                providers_by_group[group.provider_group],
                summary["usage_pct"],
                enabled=group.auto_manage,
                weekly_quota_usd=group.weekly_quota_usd,
                threshold_pct=group.disable_at_pct,
                auto_disabled_provider_ids=disabled_ids,
                set_provider_enabled=self._set_provider_enabled,
            )
            payloads.append(
                {
                    "statusSlug": group.status_slug,
                    "providerGroup": group.provider_group,
                    "usedUsd": money_float(summary["used"]),
                    "quotaUsd": money_float(group.weekly_quota_usd),
                    "remainingUsd": money_float(summary["remaining"]),
                    "usagePct": percent_float(summary["usage_pct"]),
                    "providerIds": provider_ids,
                    "autoManage": auto_manage,
                }
            )
        return payloads

    async def _set_provider_enabled(self, provider_id: int, enabled: bool) -> bool:
        try:
            await self._require_cch().set_provider_enabled(provider_id, enabled)
        except Exception:
            LOGGER.exception(
                "failed to set CCH provider enabled state",
                extra={"provider_id": provider_id, "enabled": enabled},
            )
            return False
        return True

    async def _providers_in_group(self, provider_group: str) -> list[dict[str, Any]]:
        providers = await self._require_cch().list_providers()
        return [
            provider for provider in providers
            if provider_group in provider_groups(provider)
        ]

    async def _get_group_providers(self, provider_group: str) -> list[dict[str, Any]]:
        return await self._providers_in_group(provider_group)

    async def _usage_monitor_loop(self) -> None:
        assert self._usage_manager is not None
        # Small initial delay so the HTTP client is fully ready.
        await asyncio.sleep(1)
        while True:
            delay = 1.0
            try:
                client = self._require_client()
                await self._usage_manager.poll_due(
                    client,
                    get_group_providers=self._get_group_providers,
                    set_provider_enabled=self._set_provider_enabled,
                )
                delay = self._usage_manager.seconds_until_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("provider usage monitor loop iteration failed")
            # Cap the sleep so cancellation stays responsive and a clock-skewed
            # deadline cannot park the loop indefinitely.
            await asyncio.sleep(min(max(delay, 1.0), 60.0))

    def _require_client(self) -> httpx.AsyncClient:
        if self.client is None:
            raise RuntimeError("CCH status client is not started")
        return self.client

    def _require_cch(self) -> CchClient:
        if self._cch is None:
            raise RuntimeError("CCH status client is not started")
        return self._cch
