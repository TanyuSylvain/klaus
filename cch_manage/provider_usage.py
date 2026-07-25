"""Real-provider usage monitoring: adaptive polling and CCH enable/disable."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from cch_manage.coding_plan import (
    TIER_FIVE_HOUR,
    TIER_WEEKLY_LIMIT,
    CodingPlanQuota,
    UsageTier,
    query_coding_plan_quota,
)
from cch_manage.config import UsageMonitorConfig, UsageQueryConfig
from cch_manage.utils import provider_enabled

LOGGER = logging.getLogger("llm_proxy.cch_status.provider_usage")

# The provider-usage endpoint is unauthenticated, so vendor error text (which can
# echo upstream response bodies) never leaves the process.
PUBLIC_ERROR_MESSAGE = "usage query failed"

SetProviderEnabled = Callable[[int, bool], Awaitable[bool]]
GetGroupProviders = Callable[[str], Awaitable[list[dict[str, Any]]]]


@dataclass
class UsageSnapshot:
    success: bool
    tiers: list[UsageTier] = field(default_factory=list)
    exhausted: bool = False
    max_utilization: float = 0.0
    error: str | None = None
    plan_label: str | None = None
    queried_at: str | None = None

    def to_public_tiers(self) -> list[dict[str, Any]]:
        return [
            {
                "name": tier.name,
                "utilization": round(tier.utilization, 2),
                "resetsAt": tier.resets_at,
            }
            for tier in self.tiers
        ]

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "exhausted": self.exhausted,
            "maxUtilization": self.max_utilization,
            "error": self.error,
            "planLabel": self.plan_label,
            "queriedAt": self.queried_at,
            "tiers": self.to_public_tiers(),
        }

    @classmethod
    def from_state_dict(cls, raw: dict[str, Any]) -> "UsageSnapshot":
        tiers: list[UsageTier] = []
        for item in raw.get("tiers") or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            util = item.get("utilization")
            if not isinstance(name, str):
                continue
            try:
                utilization = float(util)
            except (TypeError, ValueError):
                continue
            tiers.append(
                UsageTier(
                    name=name,
                    utilization=utilization,
                    resets_at=item.get("resetsAt") if isinstance(item.get("resetsAt"), str) else None,
                )
            )
        max_util = float(raw.get("maxUtilization") or 0.0)
        if tiers and max_util <= 0:
            max_util = max((t.utilization for t in tiers), default=0.0)
        return cls(
            success=bool(raw.get("success", True)),
            tiers=tiers,
            exhausted=bool(raw.get("exhausted", False)),
            max_utilization=max_util,
            error=raw.get("error") if isinstance(raw.get("error"), str) else None,
            plan_label=raw.get("planLabel") if isinstance(raw.get("planLabel"), str) else None,
            queried_at=raw.get("queriedAt") if isinstance(raw.get("queriedAt"), str) else None,
        )


@dataclass
class MonitorRuntimeState:
    force_disabled: bool | None = None  # None = missing/unknown (e.g. fresh key)
    last: UsageSnapshot | None = None
    current: UsageSnapshot | None = None
    next_poll_at: float = 0.0


def adaptive_interval_seconds(
    max_utilization: float,
    *,
    base_interval_seconds: int,
    min_interval_seconds: int,
    high_usage_threshold_pct: float = 90.0,
) -> float:
    u = max(0.0, min(100.0, float(max_utilization)))
    base = float(base_interval_seconds)
    minimum = float(min_interval_seconds)
    high = float(high_usage_threshold_pct)
    if high >= 100:
        return base if u < 100 else minimum
    if u < high:
        return base
    if u >= 100:
        return minimum
    return base - (base - minimum) * (u - high) / (100.0 - high)


def is_exhausted(tiers: list[UsageTier], threshold_pct: float) -> bool:
    if not tiers:
        return False
    return any(tier.utilization >= threshold_pct for tier in tiers)


def max_utilization(tiers: list[UsageTier]) -> float:
    if not tiers:
        return 0.0
    return max(tier.utilization for tier in tiers)


def snapshot_from_quota(
    quota: CodingPlanQuota,
    *,
    disable_threshold_pct: float,
) -> UsageSnapshot:
    now = datetime.now(UTC).isoformat()
    if not quota.success:
        return UsageSnapshot(
            success=False,
            tiers=[],
            exhausted=False,
            max_utilization=0.0,
            error=quota.error,
            plan_label=quota.plan_label,
            queried_at=now,
        )
    tiers = list(quota.tiers)
    return UsageSnapshot(
        success=True,
        tiers=tiers,
        exhausted=is_exhausted(tiers, disable_threshold_pct),
        max_utilization=max_utilization(tiers),
        error=None,
        plan_label=quota.plan_label,
        queried_at=now,
    )


def should_enable_providers(
    *,
    force_disabled: bool | None,
    last: UsageSnapshot | None,
    current_exhausted: bool,
) -> bool:
    """Reset path: prefer force_disabled, else last successful exhausted snapshot."""
    if current_exhausted:
        return False
    if force_disabled is True:
        return True
    if force_disabled is None and last is not None and last.success and last.exhausted:
        return True
    return False


def should_disable_providers(*, current_exhausted: bool) -> bool:
    return current_exhausted


async def apply_provider_manage(
    providers: list[dict[str, Any]],
    *,
    force_disabled: bool | None,
    last: UsageSnapshot | None,
    current: UsageSnapshot,
    set_provider_enabled: SetProviderEnabled,
) -> tuple[bool | None, dict[str, Any]]:
    """Return updated force_disabled and manage result."""
    result: dict[str, Any] = {
        "disabledProviderIds": [],
        "enabledProviderIds": [],
        "failedProviderIds": [],
        "action": "none",
    }

    if not current.success:
        return force_disabled, result

    provider_by_id = {
        int(provider["id"]): provider
        for provider in providers
        if provider.get("id") is not None
    }

    if should_disable_providers(current_exhausted=current.exhausted):
        result["action"] = "disable"
        for provider_id, provider in provider_by_id.items():
            if not provider_enabled(provider):
                continue
            if await set_provider_enabled(provider_id, False):
                result["disabledProviderIds"].append(provider_id)
            else:
                result["failedProviderIds"].append(provider_id)
        return True, result

    if should_enable_providers(
        force_disabled=force_disabled,
        last=last,
        current_exhausted=current.exhausted,
    ):
        result["action"] = "enable"
        for provider_id, provider in provider_by_id.items():
            if provider_enabled(provider):
                continue
            if await set_provider_enabled(provider_id, True):
                result["enabledProviderIds"].append(provider_id)
            else:
                result["failedProviderIds"].append(provider_id)
        return False, result

    return force_disabled, result


def status_slugs_for_provider_group(
    provider_group: str,
    status_group_provider_groups: dict[str, list[str]] | None,
) -> list[str]:
    slugs: list[str] = []
    mapping = status_group_provider_groups or {}
    for slug, groups in mapping.items():
        if provider_group in groups:
            slugs.append(slug)
    if not slugs:
        slugs.append(provider_group)
    return sorted(set(slugs))


def load_state_file(path: str | Path) -> dict[str, Any]:
    state_path = Path(path)
    if not state_path.is_file():
        return {}
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOGGER.exception("failed to load usage monitor state from %s", state_path)
        return {}
    return raw if isinstance(raw, dict) else {}


def save_state_file(path: str | Path, payload: dict[str, Any]) -> None:
    state_path = Path(path)
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError:
        LOGGER.exception("failed to save usage monitor state to %s", state_path)


class ProviderUsageManager:
    def __init__(
        self,
        monitors: list[UsageMonitorConfig],
        query_config: UsageQueryConfig,
        *,
        status_group_provider_groups: dict[str, list[str]] | None = None,
    ):
        self.monitors = list(monitors)
        self.query_config = query_config
        self.status_group_provider_groups = status_group_provider_groups
        self._runtime: dict[str, MonitorRuntimeState] = {
            monitor.provider_group: MonitorRuntimeState() for monitor in self.monitors
        }
        self._load_persisted_state()

    def _load_persisted_state(self) -> None:
        raw = load_state_file(self.query_config.state_path)
        for provider_group, state in self._runtime.items():
            entry = raw.get(provider_group)
            if not isinstance(entry, dict):
                continue
            if "forceDisabled" in entry:
                state.force_disabled = bool(entry["forceDisabled"])
            last_raw = entry.get("last")
            if isinstance(last_raw, dict):
                state.last = UsageSnapshot.from_state_dict(last_raw)
            elif entry.get("tiers") is not None or entry.get("exhausted") is not None:
                # Backward-compatible flat shape.
                state.last = UsageSnapshot.from_state_dict(entry)

    def _persist_state(self) -> None:
        payload: dict[str, Any] = {}
        for provider_group, state in self._runtime.items():
            entry: dict[str, Any] = {}
            if state.force_disabled is not None:
                entry["forceDisabled"] = state.force_disabled
            snapshot = state.current if state.current and state.current.success else state.last
            if snapshot is not None:
                entry["last"] = snapshot.to_state_dict()
            if entry:
                payload[provider_group] = entry
        save_state_file(self.query_config.state_path, payload)

    def public_payload(self) -> dict[str, Any]:
        groups: list[dict[str, Any]] = []
        now = time.monotonic()
        for monitor in self.monitors:
            state = self._runtime[monitor.provider_group]
            snapshot, stale = self._display_snapshot(state)
            next_interval = self.query_config.base_interval_seconds
            if snapshot and snapshot.success:
                next_interval = adaptive_interval_seconds(
                    snapshot.max_utilization,
                    base_interval_seconds=self.query_config.base_interval_seconds,
                    min_interval_seconds=self.query_config.min_interval_seconds,
                    high_usage_threshold_pct=self.query_config.high_usage_threshold_pct,
                )
            failed = snapshot is None or not snapshot.success
            groups.append(
                {
                    "providerGroup": monitor.provider_group,
                    "statusGroupSlugs": status_slugs_for_provider_group(
                        monitor.provider_group,
                        self.status_group_provider_groups,
                    ),
                    "provider": monitor.provider,
                    "success": not failed,
                    "stale": stale,
                    "tiers": snapshot.to_public_tiers() if snapshot else [],
                    "maxUtilization": (
                        round(snapshot.max_utilization, 2) if snapshot else None
                    ),
                    "exhausted": bool(snapshot.exhausted) if snapshot else False,
                    "forceDisabled": state.force_disabled,
                    "nextQueryIntervalSeconds": round(next_interval, 3),
                    "nextQueryInSeconds": max(0.0, round(state.next_poll_at - now, 3)),
                    "queriedAt": snapshot.queried_at if snapshot else None,
                    "planLabel": snapshot.plan_label if snapshot else None,
                    "error": PUBLIC_ERROR_MESSAGE if failed else None,
                }
            )
        return {"groups": groups}

    @staticmethod
    def _display_snapshot(state: MonitorRuntimeState) -> tuple[UsageSnapshot | None, bool]:
        """Snapshot to show, plus whether it predates a since-failed query.

        A transient network blip should not blank out the badges, so a failed
        `current` falls back to the last successful reading.
        """
        current = state.current
        if current is not None and current.success:
            return current, False
        if state.last is not None and state.last.success:
            return state.last, True
        return current or state.last, False

    def seconds_until_due(self) -> float:
        """Seconds until the earliest monitor is due, for sleeping the poll loop."""
        if not self.monitors:
            return float(self.query_config.base_interval_seconds)
        earliest = min(
            self._runtime[monitor.provider_group].next_poll_at
            for monitor in self.monitors
        )
        return max(0.0, earliest - time.monotonic())

    async def poll_due(
        self,
        client: httpx.AsyncClient,
        *,
        get_group_providers: GetGroupProviders,
        set_provider_enabled: SetProviderEnabled,
        force: bool = False,
    ) -> None:
        now = time.monotonic()
        for monitor in self.monitors:
            state = self._runtime[monitor.provider_group]
            if not force and now < state.next_poll_at:
                continue
            await self._poll_one(
                client,
                monitor,
                state,
                get_group_providers=get_group_providers,
                set_provider_enabled=set_provider_enabled,
            )

    async def _poll_one(
        self,
        client: httpx.AsyncClient,
        monitor: UsageMonitorConfig,
        state: MonitorRuntimeState,
        *,
        get_group_providers: GetGroupProviders,
        set_provider_enabled: SetProviderEnabled,
    ) -> None:
        LOGGER.info(
            "querying coding-plan usage provider=%s group=%s",
            monitor.provider,
            monitor.provider_group,
        )
        try:
            quota = await query_coding_plan_quota(
                client,
                provider=monitor.provider,
                base_url=monitor.base_url,
                api_key=monitor.api_key,
                timeout_seconds=self.query_config.request_timeout_seconds,
            )
        except Exception as exc:
            LOGGER.exception(
                "coding-plan usage query failed group=%s",
                monitor.provider_group,
            )
            state.current = UsageSnapshot(
                success=False,
                error=str(exc),
                queried_at=datetime.now(UTC).isoformat(),
            )
            state.next_poll_at = time.monotonic() + self.query_config.base_interval_seconds
            return

        current = snapshot_from_quota(
            quota,
            disable_threshold_pct=self.query_config.disable_threshold_pct,
        )
        state.current = current

        if current.success:
            try:
                providers = await get_group_providers(monitor.provider_group)
            except Exception:
                LOGGER.exception(
                    "failed to list CCH providers for group=%s",
                    monitor.provider_group,
                )
                providers = []

            new_flag, manage_result = await apply_provider_manage(
                providers,
                force_disabled=state.force_disabled,
                last=state.last,
                current=current,
                set_provider_enabled=set_provider_enabled,
            )
            state.force_disabled = new_flag
            state.last = current
            await asyncio.to_thread(self._persist_state)
            if manage_result["action"] != "none":
                LOGGER.info(
                    "usage manage group=%s action=%s disabled=%s enabled=%s failed=%s",
                    monitor.provider_group,
                    manage_result["action"],
                    manage_result["disabledProviderIds"],
                    manage_result["enabledProviderIds"],
                    manage_result["failedProviderIds"],
                )

            interval = adaptive_interval_seconds(
                current.max_utilization,
                base_interval_seconds=self.query_config.base_interval_seconds,
                min_interval_seconds=self.query_config.min_interval_seconds,
                high_usage_threshold_pct=self.query_config.high_usage_threshold_pct,
            )
        else:
            LOGGER.warning(
                "coding-plan usage query unsuccessful group=%s error=%s",
                monitor.provider_group,
                current.error,
            )
            interval = self.query_config.base_interval_seconds

        state.next_poll_at = time.monotonic() + max(1.0, interval)
