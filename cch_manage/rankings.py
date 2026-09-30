"""Weekly user and model rankings from CCH usage-log stats."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from cch_manage.user_tags import user_matches_tags
from cch_manage.utils import decimal_from_any, int_from_any, money_float


RANK_FETCH_CONCURRENCY = 8


class RankStatsClient(Protocol):
    async def list_users(self) -> list[dict[str, Any]]:
        ...

    async def list_usage_models(self) -> list[str]:
        ...

    async def get_usage_log_stats(
        self,
        *,
        user_id: int | None = None,
        model: str | None = None,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
    ) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class StoredRankRow:
    scope: str
    entity_key: str
    name: str
    request_count: int
    token_count: int
    cost_usd: Decimal


@dataclass(frozen=True)
class RankSnapshotMeta:
    week_start: datetime
    week_end: datetime
    updated_at: datetime


def parse_rank_stats(raw: Any) -> tuple[int, int, Decimal] | None:
    if not isinstance(raw, dict):
        return None
    if not any(key in raw for key in ("totalRequests", "totalCost", "totalTokens")):
        return None
    return (
        int_from_any(raw.get("totalRequests")),
        int_from_any(raw.get("totalTokens")),
        decimal_from_any(raw.get("totalCost")),
    )


def rank_sort_key(row: StoredRankRow) -> tuple[Decimal, int, int, str]:
    return (-row.cost_usd, -row.token_count, -row.request_count, row.name.casefold())


def _is_zero_usage(request_count: int, token_count: int, cost: Decimal) -> bool:
    return request_count == 0 and token_count == 0 and cost == 0


def _row_payload(row: StoredRankRow) -> dict[str, Any]:
    return {
        "name": row.name,
        "requestCount": row.request_count,
        "tokenCount": row.token_count,
        "costUsd": money_float(row.cost_usd) or 0.0,
    }


def build_rank_response(
    rows: list[StoredRankRow],
    meta: RankSnapshotMeta | None,
    *,
    generated_at: str,
) -> dict[str, Any]:
    if meta is None:
        return {
            "generatedAt": generated_at,
            "syncedAt": None,
            "weekStart": None,
            "weekEnd": None,
            "users": [],
            "models": [],
        }

    ordered = sorted(rows, key=rank_sort_key)
    return {
        "generatedAt": generated_at,
        "syncedAt": meta.updated_at.isoformat(),
        "weekStart": meta.week_start.isoformat(),
        "weekEnd": meta.week_end.isoformat(),
        "users": [_row_payload(row) for row in ordered if row.scope == "user"],
        "models": [_row_payload(row) for row in ordered if row.scope == "model"],
    }


def _rank_jobs(
    users: list[dict[str, Any]],
    models: list[str],
    user_tags: tuple[str, ...] = (),
) -> list[tuple[str, str, str, int | None, str | None]]:
    jobs: list[tuple[str, str, str, int | None, str | None]] = []
    seen_users: set[int] = set()
    for user in users:
        user_id = user.get("id")
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            continue
        if not user_matches_tags(user, user_tags):
            continue
        if user_id in seen_users:
            continue
        seen_users.add(user_id)
        name = user.get("name")
        display = name.strip() if isinstance(name, str) and name.strip() else f"用户 {user_id}"
        jobs.append(("user", str(user_id), display, user_id, None))

    seen_models: set[str] = set()
    for model in models:
        if not isinstance(model, str) or not model or model in seen_models:
            continue
        seen_models.add(model)
        jobs.append(("model", model, model, None, model))
    return jobs


async def collect_rank_rows(
    client: RankStatsClient,
    *,
    week_start: datetime,
    query_end: datetime,
    user_tags: tuple[str, ...] = (),
) -> list[StoredRankRow]:
    users, models = await asyncio.gather(client.list_users(), client.list_usage_models())
    start_ms = int(week_start.timestamp() * 1000)
    end_ms = int(query_end.timestamp() * 1000)
    jobs = _rank_jobs(users, models, user_tags)
    semaphore = asyncio.Semaphore(RANK_FETCH_CONCURRENCY)
    rows: list[StoredRankRow] = []
    errors: list[BaseException] = []

    async def fetch(
        scope: str,
        entity_key: str,
        name: str,
        user_id: int | None,
        model: str | None,
    ) -> None:
        async with semaphore:
            try:
                raw = await client.get_usage_log_stats(
                    user_id=user_id,
                    model=model,
                    start_time_ms=start_ms,
                    end_time_ms=end_ms,
                )
            except Exception as exc:
                errors.append(exc)
                return
            parsed = parse_rank_stats(raw)
            if parsed is None:
                errors.append(RuntimeError(f"CCH rank stats missing totals for {scope} {entity_key}"))
                return
            request_count, token_count, cost = parsed
            if _is_zero_usage(request_count, token_count, cost):
                return
            rows.append(
                StoredRankRow(
                    scope=scope,
                    entity_key=entity_key,
                    name=name,
                    request_count=request_count,
                    token_count=token_count,
                    cost_usd=cost,
                )
            )

    await asyncio.gather(*(fetch(*job) for job in jobs))
    if errors:
        raise RuntimeError(f"CCH rank refresh failed for {len(errors)} queries") from errors[0]
    rows.sort(key=rank_sort_key)
    return rows


async def sync_rankings(
    client: RankStatsClient,
    store: Any,
    *,
    week_start: datetime,
    week_end: datetime,
    query_end: datetime,
    updated_at: datetime,
    user_tags: tuple[str, ...] = (),
) -> int:
    rows = await collect_rank_rows(
        client,
        week_start=week_start,
        query_end=query_end,
        user_tags=user_tags,
    )
    await store.replace_all(
        rows,
        week_start=week_start,
        week_end=week_end,
        updated_at=updated_at,
    )
    return len(rows)
