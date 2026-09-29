from decimal import Decimal
from typing import Any, Awaitable, Callable

from cch_manage.client import weekly_usage_decimal
from cch_manage.utils import percent_float, provider_enabled

SetProviderEnabled = Callable[[int, bool], Awaitable[bool]]


def quota_usage_summary(
    provider_ids: list[int],
    usages_by_provider: dict[int, dict[str, Any]],
    weekly_quota_usd: Decimal,
) -> dict[str, Decimal]:
    """Sum weekly spend for one cap.

    usages_by_provider may hold other groups' providers. Only provider_ids
    count toward this cap, so a shared limit-usage batch cannot double-count.
    """
    if weekly_quota_usd <= 0:
        raise ValueError("weekly_quota_usd must be > 0")

    used = Decimal("0")
    for provider_id in provider_ids:
        used += weekly_usage_decimal(usages_by_provider[provider_id])
    return {
        "used": used,
        "remaining": weekly_quota_usd - used,
        "usage_pct": used / weekly_quota_usd * Decimal("100"),
    }


async def manage_provider_group_quota(
    providers: list[dict[str, Any]],
    usage_pct: Decimal | None,
    *,
    enabled: bool,
    weekly_quota_usd: Decimal | None,
    threshold_pct: Decimal,
    auto_disabled_provider_ids: set[int],
    set_provider_enabled: SetProviderEnabled,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "enabled": enabled,
        "thresholdPct": percent_float(threshold_pct),
        "triggered": False,
        "disabledProviderIds": [],
        "restoredProviderIds": [],
        "failedProviderIds": [],
        "pendingRestoreProviderIds": sorted(auto_disabled_provider_ids),
    }

    if not enabled:
        return result

    if weekly_quota_usd is None or weekly_quota_usd <= 0:
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
    for provider_id in list(auto_disabled_provider_ids):
        provider = provider_by_id.get(provider_id)
        if provider is None or provider_enabled(provider):
            auto_disabled_provider_ids.discard(provider_id)

    if usage_pct >= threshold_pct:
        result["triggered"] = True
        for provider_id, provider in provider_by_id.items():
            if not provider_enabled(provider):
                continue
            if await set_provider_enabled(provider_id, False):
                auto_disabled_provider_ids.add(provider_id)
                result["disabledProviderIds"].append(provider_id)
            else:
                result["failedProviderIds"].append(provider_id)
    else:
        for provider_id in list(auto_disabled_provider_ids):
            provider = provider_by_id.get(provider_id)
            if provider is None:
                auto_disabled_provider_ids.discard(provider_id)
                continue
            if provider_enabled(provider):
                auto_disabled_provider_ids.discard(provider_id)
                continue
            if await set_provider_enabled(provider_id, True):
                auto_disabled_provider_ids.discard(provider_id)
                result["restoredProviderIds"].append(provider_id)
            else:
                result["failedProviderIds"].append(provider_id)

    result["pendingRestoreProviderIds"] = sorted(auto_disabled_provider_ids)
    return result
