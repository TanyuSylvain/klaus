import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route


LOGGER = logging.getLogger("llm_proxy.cch_status")
ROOT_RELATIVE_ATTR_RE = re.compile(r'(?P<prefix>\b(?:href|src|action)=["\'])(?P<url>/[^"\']*)')
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

        return cls(
            base_url=base_url,
            admin_token=admin_token,
            locale=str(raw.get("locale", "zh-CN")).strip("/") or "zh-CN",
            status_group_slug=str(raw.get("status_group_slug", "opus")),
            provider_group=str(raw.get("provider_group", "claude")),
            timezone=str(raw.get("timezone", "Asia/Shanghai")),
            weekly_quota_usd=weekly_quota_usd,
            cache_ttl_seconds=int(raw.get("cache_ttl_seconds", 300)),
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
        self._auto_disabled_provider_ids: set[int] = set()

    def routes(self) -> list[Route]:
        return [
            Route("/cch/status", self.status_page, methods=["GET"]),
            Route("/cch/status/weekly-consumption", self.weekly_consumption, methods=["GET"]),
        ]

    async def start(self, client_kwargs: dict[str, Any]) -> None:
        self.client = httpx.AsyncClient(**client_kwargs)

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

    async def status_page(self, _: Request) -> Response:
        client = self._require_client()
        target = f"{self.config.base_url}/{self.config.locale}/status"
        upstream = await client.get(target)

        content_type = upstream.headers.get("content-type", "")
        if upstream.status_code >= 400 or "text/html" not in content_type:
            return Response(
                upstream.content,
                status_code=upstream.status_code,
                media_type=content_type or None,
            )

        html = self._rewrite_root_relative_urls(upstream.text)
        return HTMLResponse(self._inject_status_script(html))

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
            "generatedAt": datetime.now(ZoneInfo(self.config.timezone)).isoformat(),
        }

        self._cache = payload
        self._cache_expires_at = now_monotonic + max(1, self.config.cache_ttl_seconds)
        return payload

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
  const endpoint = window.location.origin + "/cch/status/weekly-consumption";
  const money = (value) => typeof value === "number"
    ? "$" + value.toLocaleString(undefined, {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }})
    : "—";
  const percent = (value) => typeof value === "number" ? value.toFixed(2) + "%" : "—";
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
  const render = (data) => {{
    const header = findGroupHeader();
    if (!header) return;
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
  }};
  const load = () => {{
    fetch(endpoint, {{ cache: "no-store" }})
      .then((response) => response.ok ? response.json() : Promise.reject(new Error("weekly consumption unavailable")))
      .then((body) => body && body.ok && render(body.data))
      .catch(() => {{}});
  }};
  load();
  window.setInterval(load, refreshMs);
}})();
</script>
"""
        if "</body>" in html:
            return html.replace("</body>", script + "</body>", 1)
        return html + script

    def _rewrite_root_relative_urls(self, html: str) -> str:
        def replace(match: re.Match[str]) -> str:
            url = match.group("url")
            if url.startswith("//"):
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


def _money_float(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value.quantize(Decimal("0.01")))


def _percent_float(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value.quantize(Decimal("0.01")))
