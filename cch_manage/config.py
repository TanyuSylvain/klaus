import hashlib
import math
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from cch_manage.coding_plan import (
    CodingPlanProvider,
    default_base_url,
    detect_provider,
    normalize_provider_name,
)
from cch_manage.utils import parse_provider_groups


def usage_monitor_key_id(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class UsageMonitorKeyConfig:
    api_key: str
    provider_names: tuple[str, ...]
    key_id: str
    weight: float = 1.0

    @classmethod
    def from_api_key(
        cls,
        api_key: str,
        provider_names: tuple[str, ...] = (),
        weight: float = 1.0,
    ) -> "UsageMonitorKeyConfig":
        return cls(
            api_key=api_key,
            provider_names=provider_names,
            key_id=usage_monitor_key_id(api_key),
            weight=weight,
        )


@dataclass(frozen=True)
class UsageMonitorConfig:
    provider_group: str
    base_url: str
    provider: CodingPlanProvider
    keys: tuple[UsageMonitorKeyConfig, ...]


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
class QuotaGroupConfig:
    """One weekly USD cap shown on a single status-page card.

    status_slug is the card (/status/<slug>). provider_group is the CCH
    provider tag whose weekly spend is summed and whose providers are
    disabled when auto_manage is on. The two names match for most cards
    and differ when the card is not named after the tag (opus vs claude).
    """

    status_slug: str
    provider_group: str
    weekly_quota_usd: Decimal
    auto_manage: bool = False
    disable_at_pct: Decimal = Decimal("100")


@dataclass(frozen=True)
class CchStatusConfig:
    base_url: str
    admin_token: str
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"
    cache_ttl_seconds: int = 300
    weekly_leaderboard_enabled: bool = False
    # Extra status-card -> provider-tag links for cards that have no quota
    # group. A quota_groups entry already links its card to its provider tag.
    card_provider_groups: dict[str, list[str]] | None = None
    quota_groups: tuple[QuotaGroupConfig, ...] = ()
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

        reject_legacy_cch_status_keys(raw)
        card_provider_groups = parse_status_group_provider_groups(
            raw.get("card_provider_groups")
        )
        usage_query = UsageQueryConfig.from_dict(
            raw.get("usage_query") if isinstance(raw.get("usage_query"), dict) else None
        )
        usage_monitors = parse_usage_monitors(raw.get("usage_monitors"))

        return cls(
            base_url=base_url,
            admin_token=admin_token,
            locale=str(raw.get("locale", "zh-CN")).strip("/") or "zh-CN",
            timezone=str(raw.get("timezone", "Asia/Shanghai")),
            cache_ttl_seconds=int(raw.get("cache_ttl_seconds", 300)),
            weekly_leaderboard_enabled=bool(raw.get("weekly_leaderboard_enabled", False)),
            card_provider_groups=card_provider_groups,
            quota_groups=parse_quota_groups(raw.get("quota_groups")),
            usage_monitors=usage_monitors,
            usage_query=usage_query,
        )

    def effective_card_provider_groups(self) -> dict[str, list[str]] | None:
        """Card -> provider tags for price, request, and token badges.

        Each quota group contributes status_slug -> provider_group. An
        explicit card_provider_groups entry can add further tags for that
        card, or name the backing tag of a card that has no weekly cap.
        """
        merged: dict[str, list[str]] = {
            slug: list(groups) for slug, groups in (self.card_provider_groups or {}).items()
        }
        for group in self.quota_groups:
            tags = merged.setdefault(group.status_slug, [])
            if group.provider_group not in tags:
                tags.append(group.provider_group)
        return merged or None


_LEGACY_CCH_STATUS_KEYS = {
    "status_group_slug": "quota_groups[].status_slug",
    "provider_group": "quota_groups[].provider_group",
    "weekly_quota_usd": "quota_groups[].weekly_quota_usd",
    "auto_manage_provider_group_quota": "quota_groups[].auto_manage",
    "auto_disable_threshold_pct": "quota_groups[].disable_at_pct",
    "status_group_provider_groups": "card_provider_groups",
}


def reject_legacy_cch_status_keys(raw: dict[str, Any]) -> None:
    found = [key for key in _LEGACY_CCH_STATUS_KEYS if key in raw]
    if not found:
        return
    moved = "\n".join(f"  {key} -> {_LEGACY_CCH_STATUS_KEYS[key]}" for key in found)
    raise ValueError(
        "cch_status still has removed settings. Weekly caps belong under "
        "quota_groups; the status-card to provider-tag map is card_provider_groups.\n"
        f"{moved}"
    )


def parse_quota_groups(value: Any) -> tuple[QuotaGroupConfig, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("cch_status.quota_groups must be a list")

    groups: list[QuotaGroupConfig] = []
    seen_slugs: set[str] = set()
    seen_provider_groups: set[str] = set()
    for index, item in enumerate(value):
        path = f"cch_status.quota_groups[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{path} must be a mapping")

        status_slug = _required_name(item.get("status_slug"), f"{path}.status_slug")
        if status_slug in seen_slugs:
            raise ValueError(
                f"{path}: duplicate status_slug {status_slug!r}; "
                "each status-page card may have at most one weekly cap"
            )
        seen_slugs.add(status_slug)

        raw_provider_group = item.get("provider_group")
        provider_group = (
            _required_name(raw_provider_group, f"{path}.provider_group")
            if raw_provider_group is not None
            else status_slug
        )
        if provider_group in seen_provider_groups:
            raise ValueError(
                f"{path}: duplicate provider_group {provider_group!r}; "
                "each provider tag may have at most one weekly cap"
            )
        seen_provider_groups.add(provider_group)

        groups.append(
            QuotaGroupConfig(
                status_slug=status_slug,
                provider_group=provider_group,
                weekly_quota_usd=_parse_positive_decimal(
                    item.get("weekly_quota_usd"),
                    f"{path}.weekly_quota_usd",
                ),
                auto_manage=bool(item.get("auto_manage", False)),
                disable_at_pct=_parse_percent(
                    item.get("disable_at_pct", 100),
                    f"{path}.disable_at_pct",
                ),
            )
        )
    return tuple(groups)


def _required_name(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} is required")
    return value.strip()


def _parse_positive_decimal(value: Any, path: str) -> Decimal:
    if value is None:
        raise ValueError(f"{path} is required")
    number = _parse_decimal(value, path)
    if number <= 0:
        raise ValueError(f"{path} must be > 0")
    return number


def _parse_percent(value: Any, path: str) -> Decimal:
    number = _parse_decimal(value, path)
    if not Decimal("0") < number <= Decimal("100"):
        raise ValueError(f"{path} must be in (0, 100]")
    return number


def _parse_decimal(value: Any, path: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError(f"{path} must be a number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{path} must be a number") from exc
    if not number.is_finite():
        raise ValueError(f"{path} must be a finite number")
    return number


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
        path = f"cch_status.usage_monitors[{index}]"
        if not provider_group:
            raise ValueError(f"{path}.provider_group is required")
        # Runtime state is keyed by provider_group; a duplicate would be silently
        # ignored rather than polled.
        if provider_group in seen_groups:
            raise ValueError(
                f"{path}: duplicate provider_group {provider_group!r}; "
                "each provider group may have at most one monitor"
            )
        seen_groups.add(provider_group)

        explicit = item.get("provider")
        if explicit is not None and str(explicit).strip():
            provider = normalize_provider_name(str(explicit).strip())
            if provider is None:
                raise ValueError(
                    f"{path}.provider must be one of: kimi, glm, minimax"
                )
        elif base_url:
            provider = detect_provider(base_url)
            if provider is None:
                raise ValueError(
                    f"{path}: cannot detect provider from base_url={base_url!r}; "
                    "set provider explicitly to kimi, glm, or minimax"
                )
        else:
            raise ValueError(
                f"{path}: set provider (kimi|glm|minimax) or base_url so the "
                "coding-plan vendor can be resolved"
            )

        if not base_url:
            base_url = default_base_url(provider)

        monitors.append(
            UsageMonitorConfig(
                provider_group=provider_group,
                base_url=base_url,
                provider=provider,
                keys=parse_usage_monitor_keys(item, path),
            )
        )
    return monitors


def parse_usage_monitor_keys(item: dict[str, Any], path: str) -> tuple[UsageMonitorKeyConfig, ...]:
    singular = str(item.get("api_key", "")).strip() if item.get("api_key") is not None else ""
    has_singular = bool(singular)
    has_list = item.get("api_keys") is not None
    if has_singular and has_list:
        raise ValueError(f"{path}: set either api_key or api_keys, not both")

    if has_list:
        if item.get("providers") is not None:
            raise ValueError(
                f"{path}: set providers on each api_keys entry, not on the monitor"
            )
        if item.get("weight") is not None:
            raise ValueError(
                f"{path}: set weight on each api_keys entry, not on the monitor"
            )
        keys = _parse_api_keys_list(item.get("api_keys"), path)
    elif has_singular:
        keys = (
            UsageMonitorKeyConfig.from_api_key(
                singular,
                parse_key_provider_names(item.get("providers"), path),
                parse_key_weight(item.get("weight"), path),
            ),
        )
    else:
        raise ValueError(f"{path}: api_key or api_keys is required")

    if len(keys) > 1:
        for key_index, key in enumerate(keys):
            if not key.provider_names:
                raise ValueError(
                    f"{path}.api_keys[{key_index}].providers is required when "
                    "a monitor has more than one api_key"
                )

    seen_names: set[str] = set()
    for key_index, key in enumerate(keys):
        for name in key.provider_names:
            if name in seen_names:
                location = (
                    f"{path}.api_keys[{key_index}]"
                    if has_list
                    else path
                )
                raise ValueError(
                    f"{location}: duplicate CCH provider name {name!r}; "
                    "each provider may be assigned to at most one api_key"
                )
            seen_names.add(name)
    return keys


def _parse_api_keys_list(value: Any, path: str) -> tuple[UsageMonitorKeyConfig, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{path}.api_keys must be a non-empty list")

    keys: list[UsageMonitorKeyConfig] = []
    seen_keys: set[str] = set()
    for index, item in enumerate(value):
        entry_path = f"{path}.api_keys[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{entry_path} must be a mapping")
        api_key = str(item.get("api_key", "")).strip()
        if not api_key:
            raise ValueError(f"{entry_path}.api_key is required")
        if api_key in seen_keys:
            raise ValueError(f"{entry_path}: duplicate api_key")
        seen_keys.add(api_key)
        keys.append(
            UsageMonitorKeyConfig.from_api_key(
                api_key,
                parse_key_provider_names(item.get("providers"), entry_path),
                parse_key_weight(item.get("weight"), entry_path),
            )
        )
    return tuple(keys)


def parse_key_weight(value: Any, path: str) -> float:
    if value is None:
        return 1.0
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{path}.weight must be a positive number")
    try:
        weight = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path}.weight must be a positive number") from exc
    if not math.isfinite(weight) or weight <= 0:
        raise ValueError(f"{path}.weight must be a finite number > 0")
    return weight


def parse_key_provider_names(value: Any, path: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{path}.providers must be a list")
    names: list[str] = []
    seen: set[str] = set()
    for item in value:
        name = str(item).strip() if item is not None else ""
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return tuple(names)
