"""Aggregate weekly CCH usage-log stats onto public status group slugs."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from cch_manage.utils import (
    decimal_from_any,
    int_from_any,
    money_float,
    provider_groups,
)


LOGGER = logging.getLogger("llm_proxy.cch_status.group_usage")

USAGE_STATS_CONCURRENCY = 8


class UsageStatsClient(Protocol):
    async def get_usage_log_stats(
        self,
        *,
        provider_id: int,
        start_time_ms: int,
        end_time_ms: int,
    ) -> dict[str, Any]:
        ...


def provider_ids_by_status_slug(
    providers: list[dict[str, Any]],
    public_groups: list[dict[str, Any]],
    status_group_provider_groups: dict[str, list[str]] | None = None,
) -> dict[str, list[int]]:
    mapping = status_group_provider_groups or {}
    ids_by_slug: dict[str, list[int]] = {}

    for public_group in public_groups:
        slug = public_group.get("publicGroupSlug")
        if not isinstance(slug, str) or not slug:
            continue

        backing = set(mapping.get(slug) or [slug])
        ids: list[int] = []
        seen: set[int] = set()
        for provider in providers:
            if not backing.intersection(provider_groups(provider)):
                continue
            provider_id = provider.get("id")
            if (
                isinstance(provider_id, int)
                and not isinstance(provider_id, bool)
                and provider_id not in seen
            ):
                seen.add(provider_id)
                ids.append(provider_id)
        if ids:
            ids_by_slug[slug] = ids

    return ids_by_slug


def parse_usage_log_stats(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    if not any(key in raw for key in ("totalRequests", "totalCost", "totalTokens")):
        return None
    return {
        "requestCount": int_from_any(raw.get("totalRequests")),
        "totalTokens": int_from_any(raw.get("totalTokens")),
        "cost": decimal_from_any(raw.get("totalCost")),
    }


def sum_usage_stats(stats_list: list[dict[str, Any]]) -> dict[str, Any]:
    request_count = 0
    total_tokens = 0
    cost = Decimal("0")
    for item in stats_list:
        request_count += int_from_any(item.get("requestCount"))
        total_tokens += int_from_any(item.get("totalTokens"))
        cost += decimal_from_any(item.get("cost"))
    return {
        "requestCount": request_count,
        "totalTokens": total_tokens,
        "costUsd": money_float(cost) or 0.0,
    }


def build_group_usage_payload(
    ids_by_slug: dict[str, list[int]],
    stats_by_provider: dict[int, dict[str, Any]],
    week_start: datetime,
    week_end: datetime,
    generated_at: datetime,
) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {}
    for slug, provider_ids in ids_by_slug.items():
        groups[slug] = sum_usage_stats(
            [
                stats_by_provider[provider_id]
                for provider_id in provider_ids
                if provider_id in stats_by_provider
            ]
        )
    return {
        "weekStart": week_start.isoformat(),
        "weekEnd": week_end.isoformat(),
        "groups": groups,
        "generatedAt": generated_at.isoformat(),
    }


async def collect_provider_usage_stats(
    cch: UsageStatsClient,
    provider_ids: list[int],
    start_time_ms: int,
    end_time_ms: int,
    *,
    concurrency: int = USAGE_STATS_CONCURRENCY,
) -> dict[int, dict[str, Any]]:
    semaphore = asyncio.Semaphore(max(1, concurrency))
    results: dict[int, dict[str, Any]] = {}

    async def fetch(provider_id: int) -> None:
        async with semaphore:
            try:
                raw = await cch.get_usage_log_stats(
                    provider_id=provider_id,
                    start_time_ms=start_time_ms,
                    end_time_ms=end_time_ms,
                )
            except Exception:
                LOGGER.exception(
                    "failed to fetch CCH usage log stats",
                    extra={"provider_id": provider_id},
                )
                return
            parsed = parse_usage_log_stats(raw)
            if parsed is None:
                LOGGER.warning(
                    "CCH usage log stats missing totals",
                    extra={"provider_id": provider_id},
                )
                return
            results[provider_id] = parsed

    await asyncio.gather(*(fetch(provider_id) for provider_id in provider_ids))
    return results
