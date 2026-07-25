from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from cch_manage.coding_plan import (
    CodingPlanProvider,
    default_base_url,
    detect_provider,
    normalize_provider_name,
)
from cch_manage.utils import parse_provider_groups


@dataclass(frozen=True)
class UsageMonitorConfig:
    provider_group: str
    base_url: str
    api_key: str
    provider: CodingPlanProvider


@dataclass(frozen=True)
class UsageQueryConfig:
    base_interval_seconds: int = 300
    min_interval_seconds: int = 30
    high_usage_threshold_pct: float = 90.0
    disable_threshold_pct: float = 100.0
    request_timeout_seconds: float = 15.0
    state_path: str = "data/cch_usage_state.json"

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "UsageQueryConfig":
        raw = raw or {}
        base = int(raw.get("base_interval_seconds", 300))
        minimum = int(raw.get("min_interval_seconds", 30))
        if base < 1:
            raise ValueError("cch_status.usage_query.base_interval_seconds must be >= 1")
        if minimum < 1:
            raise ValueError("cch_status.usage_query.min_interval_seconds must be >= 1")
        if minimum > base:
            raise ValueError(
                "cch_status.usage_query.min_interval_seconds must be <= base_interval_seconds"
            )

        high = float(raw.get("high_usage_threshold_pct", 90))
        disable = float(raw.get("disable_threshold_pct", 100))
        timeout = float(raw.get("request_timeout_seconds", 15))
        # A too-low disable threshold would take every provider in the group
        # offline on the first poll, so reject obviously wrong values early.
        if not 0 < high <= 100:
            raise ValueError(
                "cch_status.usage_query.high_usage_threshold_pct must be in (0, 100]"
            )
        if not 0 < disable <= 100:
            raise ValueError(
                "cch_status.usage_query.disable_threshold_pct must be in (0, 100]"
            )
        if high > disable:
            raise ValueError(
                "cch_status.usage_query.high_usage_threshold_pct must be "
                "<= disable_threshold_pct"
            )
        if timeout <= 0:
            raise ValueError("cch_status.usage_query.request_timeout_seconds must be > 0")

        return cls(
            base_interval_seconds=base,
            min_interval_seconds=minimum,
            high_usage_threshold_pct=high,
            disable_threshold_pct=disable,
            request_timeout_seconds=timeout,
            state_path=str(raw.get("state_path", "data/cch_usage_state.json")),
        )


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
    usage_monitors: list[UsageMonitorConfig] = field(default_factory=list)
    usage_query: UsageQueryConfig = field(default_factory=UsageQueryConfig)

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
        usage_query = UsageQueryConfig.from_dict(
            raw.get("usage_query") if isinstance(raw.get("usage_query"), dict) else None
        )
        usage_monitors = parse_usage_monitors(raw.get("usage_monitors"))

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
            usage_monitors=usage_monitors,
            usage_query=usage_query,
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


def parse_usage_monitors(value: Any) -> list[UsageMonitorConfig]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("cch_status.usage_monitors must be a list")

    monitors: list[UsageMonitorConfig] = []
    seen_groups: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"cch_status.usage_monitors[{index}] must be a mapping")

        provider_group = str(item.get("provider_group", "")).strip()
        base_url = str(item.get("base_url", "")).strip().rstrip("/")
        api_key = str(item.get("api_key", "")).strip()
        if not provider_group:
            raise ValueError(f"cch_status.usage_monitors[{index}].provider_group is required")
        if not api_key:
            raise ValueError(f"cch_status.usage_monitors[{index}].api_key is required")
        # Runtime state is keyed by provider_group; a duplicate would be silently
        # ignored rather than polled.
        if provider_group in seen_groups:
            raise ValueError(
                f"cch_status.usage_monitors[{index}]: duplicate provider_group "
                f"{provider_group!r}; each provider group may have at most one monitor"
            )
        seen_groups.add(provider_group)

        explicit = item.get("provider")
        if explicit is not None and str(explicit).strip():
            provider = normalize_provider_name(str(explicit).strip())
            if provider is None:
                raise ValueError(
                    f"cch_status.usage_monitors[{index}].provider must be one of: "
                    "kimi, glm, minimax"
                )
        elif base_url:
            provider = detect_provider(base_url)
            if provider is None:
                raise ValueError(
                    f"cch_status.usage_monitors[{index}]: cannot detect provider from "
                    f"base_url={base_url!r}; set provider explicitly to kimi, glm, or minimax"
                )
        else:
            raise ValueError(
                f"cch_status.usage_monitors[{index}]: set provider (kimi|glm|minimax) "
                "or base_url so the coding-plan vendor can be resolved"
            )

        if not base_url:
            base_url = default_base_url(provider)

        monitors.append(
            UsageMonitorConfig(
                provider_group=provider_group,
                base_url=base_url,
                api_key=api_key,
                provider=provider,
            )
        )
    return monitors
