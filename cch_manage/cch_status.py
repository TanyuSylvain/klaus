import json
import logging
import re
import time
from asyncio import Lock
from dataclasses import dataclass
from datetime import UTC, datetime, time as datetime_time, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from html import escape
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route


LOGGER = logging.getLogger("llm_proxy.cch_status")
ROOT_RELATIVE_ATTR_RE = re.compile(r'(?P<prefix>\b(?:href|src|action)=["\'])(?P<url>/[^"\']*)')


@dataclass(frozen=True)
class _PublicStatusCacheEntry:
    payload: dict[str, Any]
    status_code: int
    expires_at: float


@dataclass(frozen=True)
class CchStatusConfig:
    base_url: str
    admin_token: str
    locale: str = "zh-CN"
    status_group_slug: str = "opus"
    provider_group: str = "claude"
    timezone: str = "Asia/Shanghai"
    weekly_quota_usd: Decimal | None = None
    cache_ttl_seconds: int = 300
    weekly_leaderboard_enabled: bool = False
    status_group_provider_groups: dict[str, list[str]] | None = None
    auto_manage_provider_group_quota: bool = False
    auto_disable_threshold_pct: Decimal = Decimal("100")

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "CchStatusConfig | None":
        if not raw or not raw.get("enabled", False):
            return None

        base_url = str(raw.get("base_url", "")).rstrip("/")
        admin_token = str(raw.get("admin_token", ""))
        if not base_url or not admin_token:
            raise ValueError("cch_status.base_url and cch_status.admin_token are required when enabled")

        quota_raw = raw.get("weekly_quota_usd")
        weekly_quota_usd = Decimal(str(quota_raw)) if quota_raw is not None else None
        threshold_raw = raw.get("auto_disable_threshold_pct", 100)
        status_group_provider_groups = _parse_status_group_provider_groups(
            raw.get("status_group_provider_groups")
        )

        return cls(
            base_url=base_url,
            admin_token=admin_token,
            locale=str(raw.get("locale", "zh-CN")).strip("/") or "zh-CN",
            status_group_slug=str(raw.get("status_group_slug", "opus")),
            provider_group=str(raw.get("provider_group", "claude")),
            timezone=str(raw.get("timezone", "Asia/Shanghai")),
            weekly_quota_usd=weekly_quota_usd,
            cache_ttl_seconds=int(raw.get("cache_ttl_seconds", 300)),
            weekly_leaderboard_enabled=bool(raw.get("weekly_leaderboard_enabled", False)),
            status_group_provider_groups=status_group_provider_groups,
            auto_manage_provider_group_quota=bool(
                raw.get("auto_manage_provider_group_quota", False)
            ),
            auto_disable_threshold_pct=Decimal(str(threshold_raw)),
        )


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
                "x-llm-proxy-cch-cache-expires-in-seconds": _seconds_until(expires_at),
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

        html = self._rewrite_root_relative_urls(upstream.text)
        return HTMLResponse(
            self._inject_status_script(html),
            headers={"cache-control": "no-store"},
        )

    async def get_weekly_consumption(self) -> dict[str, Any]:
        now_monotonic = time.monotonic()
        if self._cache is not None and now_monotonic < self._cache_expires_at:
            return self._cache

        week_start, week_end = self._current_week_range()
        _, providers = await self._get_claude_group_providers()
        provider_ids = [int(provider["id"]) for provider in providers if provider.get("id") is not None]

        used = Decimal("0")
        for provider_id in provider_ids:
            provider_used = await self._get_provider_weekly_usage(provider_id)
            used += provider_used

        quota = self.config.weekly_quota_usd
        remaining = quota - used if quota is not None else None
        usage_pct = (used / quota * Decimal("100")) if quota and quota > 0 else None
        auto_manage = await self._manage_provider_group_quota(providers, usage_pct)

        payload = {
            "weekStart": week_start.isoformat(),
            "weekEnd": week_end.isoformat(),
            "providerGroup": self.config.provider_group,
            "statusGroupSlug": self.config.status_group_slug,
            "usedUsd": _money_float(used),
            "quotaUsd": _money_float(quota),
            "remainingUsd": _money_float(remaining),
            "usagePct": _percent_float(usage_pct),
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
            leaderboard = await self._get_weekly_leaderboard(week_start, week_end)

        payload = {
            "weekStart": week_start.isoformat(),
            "weekEnd": week_end.isoformat(),
            "weeklyLeaderboardEnabled": self.config.weekly_leaderboard_enabled,
            "leaderboard": [
                _public_leaderboard_entry(entry)
                for entry in _active_leaderboard_entries(leaderboard)[:3]
            ],
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
        target_hash = _api_key_hash(api_key)
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

        providers, _ = await self._get_claude_group_providers()
        public_status = await self._get_public_status_groups()
        groups = _extract_group_price_coefficients(
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

    async def _get_public_status_groups(self) -> list[dict[str, Any]]:
        data = await self._get_public_status_payload("include=groups")
        groups = data.get("groups") if isinstance(data, dict) else None
        return [group for group in groups if isinstance(group, dict)] if isinstance(groups, list) else []

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
                ttl_seconds = _public_status_ttl_seconds(
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
        client = self._require_client()
        url = f"{self.config.base_url}/api/public-status"
        if query:
            url = f"{url}?{query}"
        response = await client.get(url)
        if response.status_code not in (200, 400, 503):
            response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("CCH public status returned unexpected data shape")
        return data, response.status_code

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

    async def _get_weekly_leaderboard(
        self,
        week_start: datetime,
        week_end: datetime,
    ) -> list[dict[str, Any]]:
        tz = ZoneInfo(self.config.timezone)
        statistics_data = await self._post_action(
            "/api/actions/statistics/getUserStatistics",
            {"timeRange": "7days"},
        )
        statistics_users = _statistics_users(
            statistics_data if isinstance(statistics_data, list) else [statistics_data]
        )
        api_keys_by_user = await self._get_api_key_hashes_by_user(statistics_users)
        return _extract_weekly_leaderboard(
            statistics_data,
            week_start,
            week_end,
            tz,
            api_keys_by_user,
        )

    async def _get_api_key_hashes_by_user(
        self,
        statistics_users: list[dict[str, Any]],
    ) -> dict[str, list[str]]:
        hashes_by_user: dict[str, list[str]] = {}
        for user in statistics_users:
            user_id = user.get("id")
            data_key = user.get("dataKey")
            if user_id is None or not isinstance(data_key, str):
                continue

            keys_data = await self._post_action(
                "/api/actions/keys/getKeys",
                {"userId": int(user_id)},
            )
            key_hashes = _extract_api_key_hashes(keys_data)
            if key_hashes:
                hashes_by_user[data_key] = key_hashes
        return hashes_by_user

    async def _get_claude_group_providers(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        data = await self._post_action("/api/actions/providers/getProviders", {})
        if not isinstance(data, list):
            raise RuntimeError("CCH getProviders returned unexpected data shape")

        provider_group = self.config.provider_group
        providers = [provider for provider in data if isinstance(provider, dict)]
        return providers, [
            provider for provider in providers
            if provider_group in _provider_groups(provider)
        ]

    async def _get_provider_weekly_usage(self, provider_id: int) -> Decimal:
        data = await self._post_action(
            "/api/actions/providers/getProviderLimitUsage",
            {"providerId": provider_id},
        )
        if not isinstance(data, dict):
            raise RuntimeError("CCH getProviderLimitUsage returned unexpected data shape")

        weekly = data.get("costWeekly")
        if not isinstance(weekly, dict):
            raise RuntimeError("CCH getProviderLimitUsage response has no costWeekly object")

        current = weekly.get("current")
        try:
            used = Decimal(str(current if current is not None else "0"))
        except (InvalidOperation, ValueError) as exc:
            raise RuntimeError(f"CCH costWeekly.current is not numeric: {current!r}") from exc

        return used

    async def _manage_provider_group_quota(
        self,
        providers: list[dict[str, Any]],
        usage_pct: Decimal | None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "enabled": self.config.auto_manage_provider_group_quota,
            "thresholdPct": _percent_float(self.config.auto_disable_threshold_pct),
            "triggered": False,
            "disabledProviderIds": [],
            "restoredProviderIds": [],
            "failedProviderIds": [],
            "pendingRestoreProviderIds": sorted(self._auto_disabled_provider_ids),
        }

        if not self.config.auto_manage_provider_group_quota:
            return result

        if self.config.weekly_quota_usd is None or self.config.weekly_quota_usd <= 0:
            result["reason"] = "missing_or_invalid_quota"
            return result

        if usage_pct is None:
            result["reason"] = "usage_pct_unavailable"
            return result

        provider_by_id = {
            int(provider["id"]): provider
            for provider in providers
            if provider.get("id") is not None
        }

        # Forget stale providers and providers an admin has already re-enabled.
        for provider_id in list(self._auto_disabled_provider_ids):
            provider = provider_by_id.get(provider_id)
            if provider is None or _provider_enabled(provider):
                self._auto_disabled_provider_ids.discard(provider_id)

        threshold = self.config.auto_disable_threshold_pct
        if usage_pct >= threshold:
            result["triggered"] = True
            for provider_id, provider in provider_by_id.items():
                if not _provider_enabled(provider):
                    continue
                if await self._set_provider_enabled(provider_id, False):
                    self._auto_disabled_provider_ids.add(provider_id)
                    result["disabledProviderIds"].append(provider_id)
                else:
                    result["failedProviderIds"].append(provider_id)
        else:
            for provider_id in list(self._auto_disabled_provider_ids):
                provider = provider_by_id.get(provider_id)
                if provider is None:
                    self._auto_disabled_provider_ids.discard(provider_id)
                    continue
                if _provider_enabled(provider):
                    self._auto_disabled_provider_ids.discard(provider_id)
                    continue
                if await self._set_provider_enabled(provider_id, True):
                    self._auto_disabled_provider_ids.discard(provider_id)
                    result["restoredProviderIds"].append(provider_id)
                else:
                    result["failedProviderIds"].append(provider_id)

        result["pendingRestoreProviderIds"] = sorted(self._auto_disabled_provider_ids)
        return result

    async def _set_provider_enabled(self, provider_id: int, enabled: bool) -> bool:
        try:
            await self._post_action(
                "/api/actions/providers/editProvider",
                {"providerId": provider_id, "is_enabled": enabled},
            )
        except Exception:
            LOGGER.exception(
                "failed to set CCH provider enabled state",
                extra={"provider_id": provider_id, "enabled": enabled},
            )
            return False
        return True

    async def _post_action(self, path: str, payload: dict[str, Any]) -> Any:
        client = self._require_client()
        response = await client.post(
            self.config.base_url + path,
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {self.config.admin_token}",
                "cookie": f"auth-token={self.config.admin_token}",
            },
            json=payload,
        )
        response.raise_for_status()

        body = response.json()
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise RuntimeError(f"CCH action failed: {body.get('error', 'unknown error')}")
        return body.get("data")

    def _inject_status_script(self, html: str) -> str:
        script = f"""
<script>
(() => {{
  const statusGroupSlug = {json.dumps(self.config.status_group_slug)};
  const refreshMs = {max(1, self.config.cache_ttl_seconds) * 1000};
  const consumptionEndpoint = window.location.origin + "/cch/status/weekly-consumption";
  const leaderboardEndpoint = window.location.origin + "/cch/status/weekly-leaderboard";
  const priceCoefficientsEndpoint = window.location.origin + "/cch/status/group-price-coefficients";
  const money = (value) => typeof value === "number"
    ? "$" + value.toLocaleString(undefined, {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }})
    : "—";
  const dateLabel = (value) => {{
    const date = value ? new Date(value) : null;
    if (!date || Number.isNaN(date.getTime())) return "";
    return `${{date.getMonth() + 1}}月${{date.getDate()}}日`;
  }};
  const compactNumber = (value) => typeof value === "number"
    ? value.toLocaleString(undefined, {{ maximumFractionDigits: 0 }})
    : "0";
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({{
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }}[char]));
  const percent = (value) => typeof value === "number" ? value.toFixed(2) + "%" : "—";
  const alpha = (token, pct) => `color-mix(in oklab, var(${{token}}) ${{pct}}%, transparent)`;
  const palette = (value) => {{
    if (typeof value !== "number") {{
      return {{
        border: "hsl(var(--border))",
        background: "hsl(var(--background) / .6)",
        color: "var(--muted-foreground)",
      }};
    }}
    if (value < 70) {{
      return {{
        border: "rgb(16 185 129 / .55)",
        background: "rgb(16 185 129 / .12)",
        color: "rgb(5 150 105)",
      }};
    }}
    if (value < 100) {{
      return {{
        border: "rgb(234 179 8 / .6)",
        background: "rgb(234 179 8 / .14)",
        color: "rgb(161 98 7)",
      }};
    }}
    return {{
      border: "rgb(239 68 68 / .6)",
      background: "rgb(239 68 68 / .14)",
      color: "rgb(220 38 38)",
    }};
  }};
  const pillStyle = (colors) =>
    `border:1px solid ${{colors.border}};border-radius:6px;padding:2px 8px;background:${{colors.background}};color:${{colors.color}};`;
  const findGroupHeader = () => {{
    const link = document.querySelector(`a[href$="/status/${{statusGroupSlug}}"]`);
    if (!link) return null;
    return link.closest("div.flex.items-center.gap-2") || link.parentElement;
  }};
  const findFirstGroupCard = () => {{
    const header = findGroupHeader();
    if (!header) return null;
    return header.closest("section") || header.closest('[class*="rounded"][class*="border"]');
  }};
  const scheduleRenderQuota = (data, attempts = 20) => {{
    if (renderQuota(data) || attempts <= 0) return;
    window.setTimeout(() => scheduleRenderQuota(data, attempts - 1), 250);
  }};
  const scheduleRenderLeaderboard = (data, attempts = 20) => {{
    if (renderLeaderboard(data) || attempts <= 0) return;
    window.setTimeout(() => scheduleRenderLeaderboard(data, attempts - 1), 250);
  }};
  const scheduleRenderPriceCoefficients = (data, attempts = 20) => {{
    if (renderPriceCoefficients(data) || attempts <= 0) return;
    window.setTimeout(() => scheduleRenderPriceCoefficients(data, attempts - 1), 250);
  }};
  const trophyStyle = (rank) => {{
    if (rank === 1) return "filter:none;background:rgb(250 204 21 / .16);border-color:rgb(250 204 21 / .55);";
    if (rank === 2) return "filter:grayscale(1) brightness(1.18);background:rgb(148 163 184 / .16);border-color:rgb(148 163 184 / .55);";
    return "filter:sepia(1) saturate(1.6) hue-rotate(330deg) brightness(.88);background:rgb(180 83 9 / .14);border-color:rgb(180 83 9 / .45);";
  }};
  const renderLeaderboard = (data) => {{
    if (!data.weeklyLeaderboardEnabled) return true;
    const entries = Array.isArray(data.leaderboard) ? data.leaderboard.slice(0, 3) : [];
    const existing = document.getElementById("cch-weekly-leaderboard");
    if (entries.length === 0) {{
      if (existing) existing.remove();
      return true;
    }}

    const firstGroupCard = findFirstGroupCard();
    if (!firstGroupCard || !firstGroupCard.parentElement) return false;

    let board = document.getElementById("cch-weekly-leaderboard");
    if (!board) {{
      board = document.createElement("section");
      board.id = "cch-weekly-leaderboard";
      board.style.cssText = "margin:0 0 16px 0;";
      firstGroupCard.parentElement.insertBefore(board, firstGroupCard);
    }}

    const cards = entries.map((entry) => `
      <article style="min-width:0;border:1px solid ${{alpha("--border", 60)}};border-radius:12px;background:${{alpha("--card", 40)}};padding:16px;display:flex;align-items:center;gap:12px;backdrop-filter:blur(4px);box-shadow:0 1px 2px rgb(0 0 0 / .05);">
        <div style="width:32px;height:32px;flex:0 0 auto;border:1px solid ${{alpha("--border", 60)}};border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:18px;line-height:1;background:${{alpha("--muted", 30)}};${{trophyStyle(entry.rank)}}">🏆</div>
        <div style="min-width:0;flex:1 1 auto;display:flex;align-items:center;justify-content:space-between;gap:10px;">
          <div style="min-width:0;">
            <div style="font-size:10px;text-transform:uppercase;letter-spacing:.025em;color:var(--muted-foreground);">第${{entry.rank}}名</div>
            <div style="margin-top:2px;font-size:14px;font-weight:600;color:var(--foreground);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${{escapeHtml(entry.name || "未知用户")}}</div>
          </div>
          <div style="flex:0 0 auto;text-align:right;">
            <div style="font-size:14px;font-weight:700;color:var(--foreground);">${{money(entry.usedUsd)}}</div>
            <div style="margin-top:2px;font-size:12px;color:var(--muted-foreground);">${{compactNumber(entry.requestCount)}} 次请求</div>
          </div>
        </div>
      </article>
    `).join("");

    const rangeStart = dateLabel(data.weekStart);
    const rangeEndDate = data.weekEnd ? new Date(data.weekEnd) : null;
    if (rangeEndDate && !Number.isNaN(rangeEndDate.getTime())) {{
      rangeEndDate.setDate(rangeEndDate.getDate() - 1);
    }}
    const rangeEnd = dateLabel(rangeEndDate);
    const rangeNote = rangeStart && rangeEnd ? `（${{rangeStart}}-${{rangeEnd}}）` : "";
    board.innerHTML = `
      <div style="margin-bottom:10px;font-size:14px;font-weight:600;color:var(--foreground);">本周消费排行榜${{rangeNote}}</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;">${{cards}}</div>
    `;
    return true;
  }};
  const renderQuota = (data) => {{
    const header = findGroupHeader();
    if (!header) return false;
    let badge = document.getElementById("cch-weekly-consumption");
    if (!badge) {{
      badge = document.createElement("div");
      badge.id = "cch-weekly-consumption";
      badge.style.cssText = "display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-left:8px;font-size:12px;color:var(--muted-foreground);";
      header.appendChild(badge);
    }}
    const colors = palette(data.usagePct);
    const style = pillStyle(colors);
    badge.innerHTML = [
      `<span style="${{style}}">本周消费 ${{money(data.usedUsd)}} / ${{money(data.quotaUsd)}}</span>`,
      `<span style="${{style}}">${{percent(data.usagePct)}}</span>`,
      `<span style="${{style}}">剩余 ${{money(data.remainingUsd)}}</span>`,
    ].join("");
    return true;
  }};
  const groupSlugFromHref = (href) => {{
    const match = String(href || "").match(/\\/status\\/([^/?#]+)(?:[?#].*)?$/);
    return match ? decodeURIComponent(match[1]) : "";
  }};
  const coefficientBadgeStyle = () =>
    "border:1px solid rgb(16 185 129 / .55);border-radius:6px;background:rgb(16 185 129 / .12);padding:2px 8px;font-size:12px;line-height:1.25;color:rgb(5 150 105);";
  const renderPriceCoefficients = (data) => {{
    const groups = data && data.groups && typeof data.groups === "object" ? data.groups : null;
    if (!groups) return true;

    let renderedAny = false;
    const links = Array.from(document.querySelectorAll('a[href*="/status/"]'));
    for (const link of links) {{
      const slug = groupSlugFromHref(link.getAttribute("href"));
      if (!slug || !Array.isArray(groups[slug]) || groups[slug].length === 0) continue;
      if (!String(link.textContent || "").trim()) continue;

      const header = link.closest("div.flex.items-center.gap-2") || link.parentElement;
      if (!header) continue;
      link.style.flex = "0 1 auto";

      let badges = header.querySelector(`span[data-cch-price-coefficients="${{slug}}"]`);
      if (!badges) {{
        badges = document.createElement("span");
        badges.dataset.cchPriceCoefficients = slug;
        badges.style.cssText = "display:inline-flex;flex:0 0 auto;align-items:center;gap:4px;margin-left:6px;";
        link.insertAdjacentElement("afterend", badges);
      }}

      badges.innerHTML = groups[slug].map((value) =>
        `<span style="${{coefficientBadgeStyle()}}">${{escapeHtml(value)}}x</span>`
      ).join("");
      renderedAny = true;
    }}
    return renderedAny || document.querySelector('a[href*="/status/"]') !== null;
  }};
  const loadConsumption = () => {{
    fetch(consumptionEndpoint, {{ cache: "no-store" }})
      .then((response) => response.ok ? response.json() : Promise.reject(new Error("weekly consumption unavailable")))
      .then((body) => body && body.ok && scheduleRenderQuota(body.data))
      .catch(() => {{}});
  }};
  const loadLeaderboard = () => {{
    fetch(leaderboardEndpoint, {{ cache: "no-store" }})
      .then((response) => response.ok ? response.json() : Promise.reject(new Error("weekly leaderboard unavailable")))
      .then((body) => body && body.ok && scheduleRenderLeaderboard(body.data))
      .catch(() => {{}});
  }};
  const loadPriceCoefficients = () => {{
    fetch(priceCoefficientsEndpoint, {{ cache: "no-store" }})
      .then((response) => response.ok ? response.json() : Promise.reject(new Error("group price coefficients unavailable")))
      .then((body) => body && body.ok && scheduleRenderPriceCoefficients(body.data))
      .catch(() => {{}});
  }};
  const startEnhancements = () => {{
    loadConsumption();
    loadLeaderboard();
    loadPriceCoefficients();
    window.setInterval(loadConsumption, refreshMs);
    window.setInterval(loadLeaderboard, refreshMs);
    window.setInterval(loadPriceCoefficients, refreshMs);
  }};
  const waitForHydration = (attempts = 80) => {{
    if (window.next && document.querySelector("next-route-announcer")) {{
      startEnhancements();
      return;
    }}
    if (attempts <= 0) {{
      startEnhancements();
      return;
    }}
    window.setTimeout(() => waitForHydration(attempts - 1), 100);
  }};
  waitForHydration();
}})();
</script>
"""
        if "</body>" in html:
            return html.replace("</body>", script + "</body>", 1)
        return html + script

    def _rewrite_root_relative_urls(self, html: str) -> str:
        def replace(match: re.Match[str]) -> str:
            url = match.group("url")
            if (
                url.startswith("//")
                or url.startswith("/_next/")
                or url.startswith(f"/{self.config.locale}/status")
            ):
                return match.group(0)
            return match.group("prefix") + escape(self.config.base_url + url, quote=True)

        return ROOT_RELATIVE_ATTR_RE.sub(replace, html)

    def _require_client(self) -> httpx.AsyncClient:
        if self.client is None:
            raise RuntimeError("CCH status client is not started")
        return self.client


def _provider_groups(provider: dict[str, Any]) -> list[str]:
    groups: list[str] = []

    for field in ("groupTag", "group_tag"):
        groups.extend(_parse_provider_groups(provider.get(field)))

    raw_groups = provider.get("groups")
    if isinstance(raw_groups, list):
        for item in raw_groups:
            groups.extend(_parse_provider_groups(item))
    else:
        groups.extend(_parse_provider_groups(raw_groups))

    for field in ("groupPriorities", "group_priorities"):
        priorities = provider.get(field)
        if isinstance(priorities, dict):
            groups.extend(str(key).strip() for key in priorities if str(key).strip())

    return sorted(set(groups))


def _provider_enabled(provider: dict[str, Any]) -> bool:
    for field in ("isEnabled", "is_enabled"):
        value = provider.get(field)
        if isinstance(value, bool):
            return value
    return False


def _parse_provider_groups(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        groups: list[str] = []
        for item in value:
            groups.extend(_parse_provider_groups(item))
        return groups
    if not isinstance(value, str):
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _parse_status_group_provider_groups(value: Any) -> dict[str, list[str]] | None:
    if not isinstance(value, dict):
        return None

    parsed: dict[str, list[str]] = {}
    for raw_slug, raw_groups in value.items():
        slug = str(raw_slug).strip()
        if not slug:
            continue

        groups = _parse_provider_groups(raw_groups)
        if not groups and raw_groups is not None and not isinstance(raw_groups, list):
            group = str(raw_groups).strip()
            groups = [group] if group else []
        if groups:
            parsed[slug] = groups

    return parsed or None


def _public_status_ttl_seconds(
    payload: dict[str, Any],
    now: datetime,
    fallback_seconds: int,
) -> float:
    fresh_until = _datetime_from_iso(payload.get("freshUntil"))
    if fresh_until is not None:
        if fresh_until > now:
            return max(1.0, (fresh_until - now).total_seconds())
        return 30.0

    defaults = payload.get("defaults")
    if isinstance(defaults, dict):
        interval_minutes = defaults.get("intervalMinutes")
        if isinstance(interval_minutes, (int, float)) and interval_minutes > 0:
            return max(1.0, float(interval_minutes) * 60.0)

    return float(max(1, fallback_seconds))


def _seconds_until(monotonic_deadline: float) -> str:
    return str(max(1, int(round(monotonic_deadline - time.monotonic()))))


def _datetime_from_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _extract_group_price_coefficients(
    providers: list[dict[str, Any]],
    public_groups: list[dict[str, Any]] | None = None,
    status_group_provider_groups: dict[str, list[str]] | None = None,
) -> dict[str, list[str]]:
    if public_groups:
        return _extract_public_group_price_coefficients(
            providers,
            public_groups,
            status_group_provider_groups or {},
        )

    coefficients_by_group: dict[str, set[Decimal]] = {}

    for provider in providers:
        if not _provider_enabled(provider):
            continue

        multiplier = _decimal_from_any(provider.get("costMultiplier"))
        if multiplier <= 0:
            continue

        for group in _provider_groups(provider):
            coefficients_by_group.setdefault(group, set()).add(multiplier)

    return {
        group: [_coefficient_label(value) for value in sorted(coefficients)]
        for group, coefficients in sorted(coefficients_by_group.items())
        if coefficients
    }


def _extract_public_group_price_coefficients(
    providers: list[dict[str, Any]],
    public_groups: list[dict[str, Any]],
    status_group_provider_groups: dict[str, list[str]],
) -> dict[str, list[str]]:
    enabled_providers = [provider for provider in providers if _provider_enabled(provider)]
    coefficients_by_slug: dict[str, set[Decimal]] = {}

    for public_group in public_groups:
        slug = public_group.get("publicGroupSlug")
        if not isinstance(slug, str) or not slug:
            continue
        provider_groups = set(status_group_provider_groups.get(slug) or [slug])
        group_coefficients: set[Decimal] = set()

        for provider in enabled_providers:
            if not provider_groups.intersection(_provider_groups(provider)):
                continue
            multiplier = _decimal_from_any(provider.get("costMultiplier"))
            if multiplier > 0:
                group_coefficients.add(multiplier)

        if group_coefficients:
            coefficients_by_slug[slug] = group_coefficients

    return {
        slug: [_coefficient_label(value) for value in sorted(coefficients)]
        for slug, coefficients in sorted(coefficients_by_slug.items())
    }


def _coefficient_label(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return str(normalized.quantize(Decimal("1")))
    return format(normalized, "f")


def _extract_weekly_leaderboard(
    data: Any,
    week_start: datetime,
    week_end: datetime,
    tz: ZoneInfo,
    api_keys_by_user: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    datasets = data if isinstance(data, list) else [data]
    users = _statistics_users(datasets)
    if not users:
        return []

    api_keys_by_user = api_keys_by_user or {}
    totals: dict[str, dict[str, Any]] = {}
    for user in users:
        data_key = user["dataKey"]
        totals[data_key] = {
            "userId": user.get("id"),
            "name": user["name"],
            "used": Decimal("0"),
            "requests": 0,
            "apiKeyHashes": api_keys_by_user.get(data_key, []),
        }

    for dataset in datasets:
        if not isinstance(dataset, dict):
            continue
        chart_data = dataset.get("chartData")
        if not isinstance(chart_data, list):
            continue

        for row in chart_data:
            if not isinstance(row, dict):
                continue
            row_date = _parse_statistics_date(row.get("date"), tz)
            if row_date is None or not (week_start <= row_date < week_end):
                continue

            for data_key, total in totals.items():
                total["used"] += _decimal_from_any(row.get(f"{data_key}_cost"))
                total["requests"] += _int_from_any(row.get(f"{data_key}_calls"))

    ranked = list(totals.values())
    ranked.sort(key=lambda item: (-item["used"], -item["requests"], item["name"].casefold()))

    return [
        {
            "rank": index + 1,
            "userId": item["userId"],
            "name": item["name"],
            "usedUsd": _money_float(item["used"]),
            "requestCount": item["requests"],
            "apiKeyHashes": item["apiKeyHashes"],
        }
        for index, item in enumerate(ranked)
    ]


def _statistics_users(datasets: list[Any]) -> list[dict[str, Any]]:
    users_by_key: dict[str, dict[str, Any]] = {}
    for dataset in datasets:
        if not isinstance(dataset, dict):
            continue
        users = dataset.get("users")
        if not isinstance(users, list):
            continue
        for user in users:
            if not isinstance(user, dict):
                continue
            data_key = user.get("dataKey")
            if not isinstance(data_key, str) or not data_key:
                continue
            name = user.get("name")
            user_id = user.get("id")
            parsed_user_id = _int_from_any(user_id)
            users_by_key[data_key] = {
                "dataKey": data_key,
                "name": str(name) if name else "Unknown",
                "id": parsed_user_id if parsed_user_id > 0 else None,
            }
    return list(users_by_key.values())


def _extract_api_key_hashes(data: Any) -> list[str]:
    if not isinstance(data, list):
        return []

    key_hashes: set[str] = set()
    for key in data:
        if not isinstance(key, dict):
            continue
        api_key = key.get("key")
        if isinstance(api_key, str) and api_key:
            key_hashes.add(_api_key_hash(api_key))

    return sorted(key_hashes)


def _public_leaderboard_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in entry.items()
        if key != "apiKeyHashes"
    }


def _active_leaderboard_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        entry
        for entry in entries
        if _decimal_from_any(entry.get("usedUsd")) > 0
        or _int_from_any(entry.get("requestCount")) > 0
    ]


def _api_key_hash(api_key: str) -> str:
    return sha256(api_key.encode("utf-8")).hexdigest()


def _parse_statistics_date(value: Any, tz: ZoneInfo) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None

    text = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return _parse_daily_statistics_date(text, tz)

        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _parse_daily_statistics_date(value: str, tz: ZoneInfo) -> datetime:
    day = datetime.fromisoformat(value).date()
    offset = tz.utcoffset(datetime.combine(day, datetime_time.min, tzinfo=UTC))
    if offset and offset > timedelta():
        day += timedelta(days=1)
    return datetime.combine(day, datetime_time.min, tzinfo=tz)


def _decimal_from_any(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _int_from_any(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _money_float(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value.quantize(Decimal("0.01")))


def _percent_float(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value.quantize(Decimal("0.01")))
