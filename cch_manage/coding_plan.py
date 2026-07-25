"""Query coding-plan 5h/weekly usage from Kimi, GLM (Zhipu), and MiniMax.

Protocols ported from cc-switch `src-tauri/src/services/coding_plan.rs`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

CodingPlanProvider = Literal["kimi", "glm", "minimax"]

TIER_FIVE_HOUR = "five_hour"
TIER_WEEKLY_LIMIT = "weekly_limit"

# Default usage-query hosts (same as cc-switch coding_plan defaults).
# Override via config base_url only for regional variants (e.g. api.z.ai, api.minimax.io).
DEFAULT_BASE_URLS: dict[CodingPlanProvider, str] = {
    "kimi": "https://api.kimi.com/coding",
    "glm": "https://open.bigmodel.cn",
    "minimax": "https://api.minimaxi.com",
}


def default_base_url(provider: CodingPlanProvider) -> str:
    return DEFAULT_BASE_URLS[provider]


@dataclass(frozen=True)
class UsageTier:
    name: str
    utilization: float
    resets_at: str | None = None


@dataclass(frozen=True)
class CodingPlanQuota:
    success: bool
    provider: CodingPlanProvider
    tiers: list[UsageTier]
    error: str | None = None
    plan_label: str | None = None


def normalize_provider_name(value: str) -> CodingPlanProvider | None:
    key = value.strip().lower()
    if key in {"kimi"}:
        return "kimi"
    if key in {"glm", "zhipu", "zhipu_cn", "zhipu_en"}:
        return "glm"
    if key in {"minimax", "minimax_cn", "minimax_en"}:
        return "minimax"
    return None


def detect_provider(base_url: str) -> CodingPlanProvider | None:
    url = base_url.lower()
    if "api.kimi.com/coding" in url:
        return "kimi"
    if "bigmodel.cn" in url or "api.z.ai" in url:
        return "glm"
    if "api.minimaxi.com" in url or "api.minimax.io" in url:
        return "minimax"
    return None


def extract_reset_time(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = int(value)
        if n <= 0:
            return None
        ms = n if n >= 1_000_000_000_000 else n * 1000
        return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()
    return None


def parse_f64(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


async def query_coding_plan_quota(
    client: httpx.AsyncClient,
    *,
    provider: CodingPlanProvider,
    base_url: str,
    api_key: str,
    timeout_seconds: float = 15.0,
) -> CodingPlanQuota:
    try:
        if provider == "kimi":
            return await _query_kimi(client, base_url, api_key, timeout_seconds)
        if provider == "glm":
            return await _query_zhipu(client, base_url, api_key, timeout_seconds)
        if provider == "minimax":
            return await _query_minimax(client, base_url, api_key, timeout_seconds)
    except httpx.HTTPError as exc:
        return CodingPlanQuota(
            success=False,
            provider=provider,
            tiers=[],
            error=f"Network error: {exc}",
        )
    return CodingPlanQuota(
        success=False,
        provider=provider,
        tiers=[],
        error=f"Unknown provider: {provider}",
    )


def kimi_usages_url(base_url: str) -> str:
    # The usage endpoint lives under the coding-plan prefix; tolerate a base_url
    # given as the bare host so a regional override still resolves.
    root = (base_url or DEFAULT_BASE_URLS["kimi"]).rstrip("/")
    if not root.endswith("/coding"):
        root = f"{root}/coding"
    return f"{root}/v1/usages"


async def _query_kimi(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    timeout_seconds: float,
) -> CodingPlanQuota:
    response = await client.get(
        kimi_usages_url(base_url),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        timeout=timeout_seconds,
    )
    if response.status_code in (401, 403):
        return CodingPlanQuota(
            success=False,
            provider="kimi",
            tiers=[],
            error=f"Authentication failed (HTTP {response.status_code})",
        )
    if response.status_code >= 400:
        return CodingPlanQuota(
            success=False,
            provider="kimi",
            tiers=[],
            error=f"API error (HTTP {response.status_code}): {response.text[:500]}",
        )

    try:
        body = response.json()
    except ValueError as exc:
        return CodingPlanQuota(
            success=False,
            provider="kimi",
            tiers=[],
            error=f"Failed to parse response: {exc}",
        )
    if not isinstance(body, dict):
        return CodingPlanQuota(
            success=False,
            provider="kimi",
            tiers=[],
            error="Unexpected response shape",
        )
    return CodingPlanQuota(
        success=True,
        provider="kimi",
        tiers=parse_kimi_tiers(body),
    )


def parse_kimi_tiers(body: dict[str, Any]) -> list[UsageTier]:
    tiers: list[UsageTier] = []
    limits = body.get("limits")
    if isinstance(limits, list):
        for limit_item in limits:
            if not isinstance(limit_item, dict):
                continue
            detail = limit_item.get("detail")
            if not isinstance(detail, dict):
                continue
            limit = parse_f64(detail.get("limit")) or 1.0
            remaining = parse_f64(detail.get("remaining")) or 0.0
            used = max(limit - remaining, 0.0)
            utilization = (used / limit) * 100.0 if limit > 0 else 0.0
            tiers.append(
                UsageTier(
                    name=TIER_FIVE_HOUR,
                    utilization=utilization,
                    resets_at=extract_reset_time(detail.get("resetTime")),
                )
            )

    usage = body.get("usage")
    if isinstance(usage, dict):
        limit = parse_f64(usage.get("limit")) or 1.0
        remaining = parse_f64(usage.get("remaining")) or 0.0
        used = max(limit - remaining, 0.0)
        utilization = (used / limit) * 100.0 if limit > 0 else 0.0
        tiers.append(
            UsageTier(
                name=TIER_WEEKLY_LIMIT,
                utilization=utilization,
                resets_at=extract_reset_time(usage.get("resetTime")),
            )
        )
    return tiers


def zhipu_quota_host(base_url: str) -> str:
    if "bigmodel.cn" in base_url.lower():
        return "https://open.bigmodel.cn"
    return "https://api.z.ai"


async def _query_zhipu(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    timeout_seconds: float,
) -> CodingPlanQuota:
    url = f"{zhipu_quota_host(base_url)}/api/monitor/usage/quota/limit"
    response = await client.get(
        url,
        headers={
            "Authorization": api_key,  # Zhipu does not use Bearer prefix
            "Content-Type": "application/json",
            "Accept-Language": "en-US,en",
        },
        timeout=timeout_seconds,
    )
    if response.status_code in (401, 403):
        return CodingPlanQuota(
            success=False,
            provider="glm",
            tiers=[],
            error=f"Authentication failed (HTTP {response.status_code})",
        )
    if response.status_code >= 400:
        return CodingPlanQuota(
            success=False,
            provider="glm",
            tiers=[],
            error=f"API error (HTTP {response.status_code}): {response.text[:500]}",
        )

    try:
        body = response.json()
    except ValueError as exc:
        return CodingPlanQuota(
            success=False,
            provider="glm",
            tiers=[],
            error=f"Failed to parse response: {exc}",
        )
    if not isinstance(body, dict):
        return CodingPlanQuota(
            success=False,
            provider="glm",
            tiers=[],
            error="Unexpected response shape",
        )
    return zhipu_quota_from_body(body)


def classify_zhipu_window(item: dict[str, Any]) -> str | None:
    unit = item.get("unit")
    try:
        unit_i = int(unit) if unit is not None else None
    except (TypeError, ValueError):
        return None
    if unit_i == 3:
        return TIER_FIVE_HOUR
    if unit_i == 6:
        return TIER_WEEKLY_LIMIT
    return None


def parse_zhipu_token_tiers(data: dict[str, Any]) -> list[UsageTier]:
    five_hour: tuple[int | None, float, str | None] | None = None
    weekly: tuple[int | None, float, str | None] | None = None
    unclassified: list[tuple[int | None, float, str | None]] = []

    limits = data.get("limits")
    if not isinstance(limits, list):
        return []

    for limit_item in limits:
        if not isinstance(limit_item, dict):
            continue
        limit_type = str(limit_item.get("type") or "")
        if limit_type.upper() != "TOKENS_LIMIT":
            continue
        percentage = parse_f64(limit_item.get("percentage")) or 0.0
        reset_ms_raw = limit_item.get("nextResetTime")
        try:
            reset_ms = int(reset_ms_raw) if reset_ms_raw is not None else None
        except (TypeError, ValueError):
            reset_ms = None
        reset_iso = extract_reset_time(reset_ms)
        entry = (reset_ms, percentage, reset_iso)
        window = classify_zhipu_window(limit_item)
        if window == TIER_FIVE_HOUR and five_hour is None:
            five_hour = entry
        elif window == TIER_WEEKLY_LIMIT and weekly is None:
            weekly = entry
        else:
            unclassified.append(entry)

    unclassified.sort(key=lambda item: (item[0] is not None, item[0] if item[0] is not None else -(10**18)))
    for entry in unclassified:
        if five_hour is None:
            five_hour = entry
        elif weekly is None:
            weekly = entry

    tiers: list[UsageTier] = []
    for name, slot in ((TIER_FIVE_HOUR, five_hour), (TIER_WEEKLY_LIMIT, weekly)):
        if slot is None:
            continue
        _, percentage, resets_at = slot
        tiers.append(UsageTier(name=name, utilization=percentage, resets_at=resets_at))
    return tiers


def zhipu_quota_from_body(body: dict[str, Any]) -> CodingPlanQuota:
    if body.get("success") is False:
        msg = body.get("msg") or "Unknown error"
        return CodingPlanQuota(
            success=False,
            provider="glm",
            tiers=[],
            error=f"API error: {msg}",
        )
    data = body.get("data")
    if not isinstance(data, dict):
        return CodingPlanQuota(
            success=False,
            provider="glm",
            tiers=[],
            error="Missing 'data' field in response",
        )
    level = data.get("level")
    plan_label = str(level) if isinstance(level, str) and level else None
    return CodingPlanQuota(
        success=True,
        provider="glm",
        tiers=parse_zhipu_token_tiers(data),
        plan_label=plan_label,
    )


def minimax_api_domain(base_url: str) -> str:
    url = base_url.lower()
    if "api.minimax.io" in url:
        return "api.minimax.io"
    return "api.minimaxi.com"


async def _query_minimax(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    timeout_seconds: float,
) -> CodingPlanQuota:
    domain = minimax_api_domain(base_url)
    url = f"https://{domain}/v1/api/openplatform/coding_plan/remains"
    response = await client.get(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        timeout=timeout_seconds,
    )
    if response.status_code in (401, 403):
        return CodingPlanQuota(
            success=False,
            provider="minimax",
            tiers=[],
            error=f"Authentication failed (HTTP {response.status_code})",
        )
    if response.status_code >= 400:
        return CodingPlanQuota(
            success=False,
            provider="minimax",
            tiers=[],
            error=f"API error (HTTP {response.status_code}): {response.text[:500]}",
        )

    try:
        body = response.json()
    except ValueError as exc:
        return CodingPlanQuota(
            success=False,
            provider="minimax",
            tiers=[],
            error=f"Failed to parse response: {exc}",
        )
    if not isinstance(body, dict):
        return CodingPlanQuota(
            success=False,
            provider="minimax",
            tiers=[],
            error="Unexpected response shape",
        )

    base_resp = body.get("base_resp")
    if isinstance(base_resp, dict):
        status_code = base_resp.get("status_code", -1)
        try:
            code = int(status_code)
        except (TypeError, ValueError):
            code = -1
        if code != 0:
            msg = base_resp.get("status_msg") or "Unknown error"
            return CodingPlanQuota(
                success=False,
                provider="minimax",
                tiers=[],
                error=f"API error (code {code}): {msg}",
            )

    return CodingPlanQuota(
        success=True,
        provider="minimax",
        tiers=parse_minimax_tiers(body),
    )


def parse_minimax_tiers(body: dict[str, Any]) -> list[UsageTier]:
    model_remains = body.get("model_remains")
    if not isinstance(model_remains, list):
        return []

    item = next(
        (
            entry
            for entry in model_remains
            if isinstance(entry, dict) and entry.get("model_name") == "general"
        ),
        None,
    )
    if item is None:
        return []

    tiers: list[UsageTier] = []
    remain_pct = parse_f64(item.get("current_interval_remaining_percent"))
    if remain_pct is not None:
        tiers.append(
            UsageTier(
                name=TIER_FIVE_HOUR,
                utilization=100.0 - remain_pct,
                resets_at=extract_reset_time(item.get("end_time")),
            )
        )

    weekly_status = item.get("current_weekly_status")
    try:
        weekly_status_i = int(weekly_status) if weekly_status is not None else None
    except (TypeError, ValueError):
        weekly_status_i = None
    if weekly_status_i == 1:
        weekly_remain = parse_f64(item.get("current_weekly_remaining_percent"))
        if weekly_remain is not None:
            tiers.append(
                UsageTier(
                    name=TIER_WEEKLY_LIMIT,
                    utilization=100.0 - weekly_remain,
                    resets_at=extract_reset_time(item.get("weekly_end_time")),
                )
            )
    return tiers
