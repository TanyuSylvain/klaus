from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from cch_manage.utils import parse_provider_groups


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
        status_group_provider_groups = parse_status_group_provider_groups(
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


def parse_status_group_provider_groups(value: Any) -> dict[str, list[str]] | None:
    if not isinstance(value, dict):
        return None

    parsed: dict[str, list[str]] = {}
    for raw_slug, raw_groups in value.items():
        slug = str(raw_slug).strip()
        if not slug:
            continue

        groups = parse_provider_groups(raw_groups)
        if not groups and raw_groups is not None and not isinstance(raw_groups, list):
            group = str(raw_groups).strip()
            groups = [group] if group else []
        if groups:
            parsed[slug] = groups

    return parsed or None
