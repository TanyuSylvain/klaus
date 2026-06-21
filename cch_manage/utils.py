import time
from datetime import UTC, datetime, time as datetime_time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo
import re


def provider_groups(provider: dict[str, Any]) -> list[str]:
    groups: list[str] = []

    for field in ("groupTag", "group_tag"):
        groups.extend(parse_provider_groups(provider.get(field)))

    raw_groups = provider.get("groups")
    if isinstance(raw_groups, list):
        for item in raw_groups:
            groups.extend(parse_provider_groups(item))
    else:
        groups.extend(parse_provider_groups(raw_groups))

    for field in ("groupPriorities", "group_priorities"):
        priorities = provider.get(field)
        if isinstance(priorities, dict):
            groups.extend(str(key).strip() for key in priorities if str(key).strip())

    return sorted(set(groups))


def provider_enabled(provider: dict[str, Any]) -> bool:
    for field in ("isEnabled", "is_enabled"):
        value = provider.get(field)
        if isinstance(value, bool):
            return value
    return False


def parse_provider_groups(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        groups: list[str] = []
        for item in value:
            groups.extend(parse_provider_groups(item))
        return groups
    if not isinstance(value, str):
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def public_status_ttl_seconds(
    payload: dict[str, Any],
    now: datetime,
    fallback_seconds: int,
) -> float:
    fresh_until = datetime_from_iso(payload.get("freshUntil"))
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


def seconds_until(monotonic_deadline: float) -> str:
    return str(max(1, int(round(monotonic_deadline - time.monotonic()))))


def datetime_from_iso(value: Any) -> datetime | None:
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


def parse_statistics_date(value: Any, tz: ZoneInfo) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None

    text = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return parse_daily_statistics_date(text, tz)

        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def parse_daily_statistics_date(value: str, tz: ZoneInfo) -> datetime:
    day = datetime.fromisoformat(value).date()
    offset = tz.utcoffset(datetime.combine(day, datetime_time.min, tzinfo=UTC))
    if offset and offset > timedelta():
        day += timedelta(days=1)
    return datetime.combine(day, datetime_time.min, tzinfo=tz)


def decimal_from_any(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def int_from_any(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def money_float(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value.quantize(Decimal("0.01")))


def percent_float(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value.quantize(Decimal("0.01")))
