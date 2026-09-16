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
    CodingPlanQuota,
    UsageTier,
    query_coding_plan_quota,
)
from cch_manage.config import UsageMonitorConfig, UsageMonitorKeyConfig, UsageQueryConfig
from cch_manage.utils import cch_provider_name, datetime_from_iso, provider_enabled

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


def earliest_resets_at(values: list[str]) -> str | None:
    best_raw: str | None = None
    best_dt = None
    for raw in values:
        parsed = datetime_from_iso(raw)
        if parsed is None:
            continue
        if best_dt is None or parsed < best_dt:
            best_dt = parsed
            best_raw = raw
    return best_raw


def average_snapshots(
    snapshots: list[UsageSnapshot],
    *,
    disable_threshold_pct: float,
    weights: list[float] | None = None,
) -> UsageSnapshot | None:
    if weights is None:
        resolved_weights = [1.0] * len(snapshots)
    elif len(weights) != len(snapshots):
        raise ValueError("average_snapshots weights must match snapshots")
    else:
        resolved_weights = weights

    successful = [
        (snapshot, weight)
        for snapshot, weight in zip(snapshots, resolved_weights, strict=True)
        if snapshot.success
    ]
    if not successful:
        return None

    tier_order: list[str] = []
    by_name: dict[str, list[tuple[UsageTier, float]]] = {}
    for snapshot, weight in successful:
        for tier in snapshot.tiers:
            if tier.name not in by_name:
                by_name[tier.name] = []
                tier_order.append(tier.name)
            by_name[tier.name].append((tier, weight))

    averaged: list[UsageTier] = []
    for name in tier_order:
        items = by_name[name]
        total_weight = sum(weight for _, weight in items)
        utilization = (
            sum(tier.utilization * weight for tier, weight in items) / total_weight
            if total_weight > 0
            else 0.0
        )
        resets = [tier.resets_at for tier, _ in items if tier.resets_at]
        averaged.append(
            UsageTier(
                name=name,
                utilization=utilization,
                resets_at=earliest_resets_at(resets),
            )
        )

    queried_at = None
    queried_values = [
        snapshot.queried_at for snapshot, _ in successful if snapshot.queried_at
    ]
    if queried_values:
        queried_at = max(queried_values, key=_queried_at_sort_key)

    labels = {snapshot.plan_label for snapshot, _ in successful}
    plan_label = next(iter(labels)) if len(labels) == 1 else None

    return UsageSnapshot(
        success=True,
        tiers=averaged,
        exhausted=is_exhausted(averaged, disable_threshold_pct),
        max_utilization=max_utilization(averaged),
        error=None,
        plan_label=plan_label,
        queried_at=queried_at,
    )


def _queried_at_sort_key(value: str) -> datetime:
    parsed = datetime_from_iso(value)
    return parsed if parsed is not None else datetime.min.replace(tzinfo=UTC)


def filter_providers_by_names(
    providers: list[dict[str, Any]],
    provider_names: tuple[str, ...],
) -> list[dict[str, Any]]:
    if not provider_names:
        return list(providers)
    wanted = set(provider_names)
    return [provider for provider in providers if cch_provider_name(provider) in wanted]


def missing_provider_names(
    providers: list[dict[str, Any]],
    provider_names: tuple[str, ...],
) -> list[str]:
    if not provider_names:
        return []
    present = {cch_provider_name(provider) for provider in providers}
    return [name for name in provider_names if name not in present]


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
        self._runtime: dict[tuple[str, str], MonitorRuntimeState] = {
            (monitor.provider_group, key.key_id): MonitorRuntimeState()
            for monitor in self.monitors
            for key in monitor.keys
        }
        self._load_persisted_state()

    def _iter_key_states(self):
        for monitor in self.monitors:
            for key in monitor.keys:
                yield monitor, key, self._runtime[(monitor.provider_group, key.key_id)]

    def _load_persisted_state(self) -> None:
        raw = load_state_file(self.query_config.state_path)
        for monitor in self.monitors:
            entry = raw.get(monitor.provider_group)
            if not isinstance(entry, dict):
                continue
            keys_raw = entry.get("keys")
            if isinstance(keys_raw, dict):
                for key in monitor.keys:
                    key_entry = keys_raw.get(key.key_id)
                    if isinstance(key_entry, dict):
                        self._apply_state_entry(
                            self._runtime[(monitor.provider_group, key.key_id)],
                            key_entry,
                        )
                continue
            # Legacy flat shape: only attributable when the monitor has one key.
            if len(monitor.keys) == 1:
                self._apply_state_entry(
                    self._runtime[(monitor.provider_group, monitor.keys[0].key_id)],
                    entry,
                )

    @staticmethod
    def _apply_state_entry(state: MonitorRuntimeState, entry: dict[str, Any]) -> None:
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
        for monitor in self.monitors:
            keys_payload: dict[str, Any] = {}
            for key in monitor.keys:
                state = self._runtime[(monitor.provider_group, key.key_id)]
                entry: dict[str, Any] = {}
                if state.force_disabled is not None:
                    entry["forceDisabled"] = state.force_disabled
                snapshot = (
                    state.current if state.current and state.current.success else state.last
                )
                if snapshot is not None:
                    entry["last"] = snapshot.to_state_dict()
                if entry:
                    keys_payload[key.key_id] = entry
            if keys_payload:
                payload[monitor.provider_group] = {"keys": keys_payload}
        save_state_file(self.query_config.state_path, payload)

    def public_payload(self) -> dict[str, Any]:
        groups: list[dict[str, Any]] = []
        now = time.monotonic()
        for monitor in self.monitors:
            display_snapshots: list[UsageSnapshot] = []
            display_weights: list[float] = []
            key_usages: list[dict[str, Any]] = []
            stale_any = False
            force_flags: list[bool | None] = []
            soonest_state: MonitorRuntimeState | None = None
            for key in monitor.keys:
                state = self._runtime[(monitor.provider_group, key.key_id)]
                snapshot, stale = self._display_snapshot(state)
                force_flags.append(state.force_disabled)
                if soonest_state is None or state.next_poll_at < soonest_state.next_poll_at:
                    soonest_state = state
                if stale or (state.current is not None and not state.current.success):
                    stale_any = True
                key_usages.append(self._public_key_usage(snapshot, stale))
                if snapshot is not None and snapshot.success:
                    display_snapshots.append(snapshot)
                    display_weights.append(key.weight)

            snapshot = average_snapshots(
                display_snapshots,
                disable_threshold_pct=self.query_config.disable_threshold_pct,
                weights=display_weights,
            )
            failed = snapshot is None
            next_interval = float(self.query_config.base_interval_seconds)
            soonest_display, _ = (
                self._display_snapshot(soonest_state)
                if soonest_state is not None
                else (None, False)
            )
            interval_source = (
                soonest_display if soonest_display and soonest_display.success else snapshot
            )
            if interval_source is not None and interval_source.success:
                next_interval = adaptive_interval_seconds(
                    interval_source.max_utilization,
                    base_interval_seconds=self.query_config.base_interval_seconds,
                    min_interval_seconds=self.query_config.min_interval_seconds,
                    high_usage_threshold_pct=self.query_config.high_usage_threshold_pct,
                )
            next_poll_at = soonest_state.next_poll_at if soonest_state is not None else now
            groups.append(
                {
                    "providerGroup": monitor.provider_group,
                    "statusGroupSlugs": status_slugs_for_provider_group(
                        monitor.provider_group,
                        self.status_group_provider_groups,
                    ),
                    "provider": monitor.provider,
                    "success": not failed,
                    "stale": (not failed) and stale_any,
                    "tiers": snapshot.to_public_tiers() if snapshot else [],
                    "keyUsages": key_usages,
                    "maxUtilization": (
                        round(snapshot.max_utilization, 2) if snapshot else None
                    ),
                    "exhausted": bool(snapshot.exhausted) if snapshot else False,
                    "forceDisabled": _aggregate_force_disabled(force_flags),
                    "nextQueryIntervalSeconds": round(next_interval, 3),
                    "nextQueryInSeconds": max(0.0, round(next_poll_at - now, 3)),
                    "queriedAt": snapshot.queried_at if snapshot else None,
                    "planLabel": snapshot.plan_label if snapshot else None,
                    "error": PUBLIC_ERROR_MESSAGE if failed else None,
                }
            )
        return {"groups": groups}

    @staticmethod
    def _public_key_usage(
        snapshot: UsageSnapshot | None,
        stale: bool,
    ) -> dict[str, Any]:
        if snapshot is not None and snapshot.success:
            return {
                "success": True,
                "stale": stale,
                "tiers": snapshot.to_public_tiers(),
                "error": None,
            }
        return {
            "success": False,
            "stale": False,
            "tiers": [],
            "error": PUBLIC_ERROR_MESSAGE,
        }

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
        """Seconds until the earliest key is due, for sleeping the poll loop."""
        if not self._runtime:
            return float(self.query_config.base_interval_seconds)
        earliest = min(state.next_poll_at for state in self._runtime.values())
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
        for monitor, key, state in self._iter_key_states():
            if not force and now < state.next_poll_at:
                continue
            await self._poll_one(
                client,
                monitor,
                key,
                state,
                get_group_providers=get_group_providers,
                set_provider_enabled=set_provider_enabled,
            )

    async def _poll_one(
        self,
        client: httpx.AsyncClient,
        monitor: UsageMonitorConfig,
        key: UsageMonitorKeyConfig,
        state: MonitorRuntimeState,
        *,
        get_group_providers: GetGroupProviders,
        set_provider_enabled: SetProviderEnabled,
    ) -> None:
        LOGGER.info(
            "querying coding-plan usage provider=%s group=%s key=%s",
            monitor.provider,
            monitor.provider_group,
            key.key_id,
        )
        try:
            quota = await query_coding_plan_quota(
                client,
                provider=monitor.provider,
                base_url=monitor.base_url,
                api_key=key.api_key,
                timeout_seconds=self.query_config.request_timeout_seconds,
            )
        except Exception as exc:
            LOGGER.exception(
                "coding-plan usage query failed group=%s key=%s",
                monitor.provider_group,
                key.key_id,
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

            missing = missing_provider_names(providers, key.provider_names)
            if missing:
                LOGGER.warning(
                    "usage monitor group=%s key=%s providers not found in CCH: %s",
                    monitor.provider_group,
                    key.key_id,
                    missing,
                )
            managed = filter_providers_by_names(providers, key.provider_names)

            new_flag, manage_result = await apply_provider_manage(
                managed,
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
                    "usage manage group=%s key=%s action=%s disabled=%s enabled=%s failed=%s",
                    monitor.provider_group,
                    key.key_id,
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
                "coding-plan usage query unsuccessful group=%s key=%s error=%s",
                monitor.provider_group,
                key.key_id,
                current.error,
            )
            interval = self.query_config.base_interval_seconds

        state.next_poll_at = time.monotonic() + max(1.0, interval)


def _aggregate_force_disabled(flags: list[bool | None]) -> bool | None:
    if any(flag is True for flag in flags):
        return True
    if flags and all(flag is None for flag in flags):
        return None
    if any(flag is False for flag in flags):
        return False
    return None
