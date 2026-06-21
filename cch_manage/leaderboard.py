from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from cch_manage.utils import (
    decimal_from_any,
    int_from_any,
    money_float,
    parse_statistics_date,
)

PostAction = Callable[[str, dict[str, Any]], Awaitable[Any]]


async def get_weekly_leaderboard(
    post: PostAction,
    week_start: datetime,
    week_end: datetime,
    tz: ZoneInfo,
) -> list[dict[str, Any]]:
    statistics_data = await post(
        "/api/actions/statistics/getUserStatistics",
        {"timeRange": "7days"},
    )
    statistics_users = statistics_users_from_datasets(
        statistics_data if isinstance(statistics_data, list) else [statistics_data]
    )
    api_keys_by_user = await get_api_key_hashes_by_user(post, statistics_users)
    return extract_weekly_leaderboard(
        statistics_data,
        week_start,
        week_end,
        tz,
        api_keys_by_user,
    )


async def get_api_key_hashes_by_user(
    post: PostAction,
    statistics_users: list[dict[str, Any]],
) -> dict[str, list[str]]:
    hashes_by_user: dict[str, list[str]] = {}
    for user in statistics_users:
        user_id = user.get("id")
        data_key = user.get("dataKey")
        if user_id is None or not isinstance(data_key, str):
            continue

        keys_data = await post(
            "/api/actions/keys/getKeys",
            {"userId": int(user_id)},
        )
        key_hashes = extract_api_key_hashes(keys_data)
        if key_hashes:
            hashes_by_user[data_key] = key_hashes
    return hashes_by_user


def extract_weekly_leaderboard(
    data: Any,
    week_start: datetime,
    week_end: datetime,
    tz: ZoneInfo,
    api_keys_by_user: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    datasets = data if isinstance(data, list) else [data]
    users = statistics_users_from_datasets(datasets)
    if not users:
        return []

    api_keys_by_user = api_keys_by_user or {}
    totals: dict[str, dict[str, Any]] = {}
    for user in users:
        data_key = user["dataKey"]
        totals[data_key] = {
            "userId": user.get("id"),
            "name": user["name"],
            "used": Decimal("0"),
            "requests": 0,
            "apiKeyHashes": api_keys_by_user.get(data_key, []),
        }

    for dataset in datasets:
        if not isinstance(dataset, dict):
            continue
        chart_data = dataset.get("chartData")
        if not isinstance(chart_data, list):
            continue

        for row in chart_data:
            if not isinstance(row, dict):
                continue
            row_date = parse_statistics_date(row.get("date"), tz)
            if row_date is None or not (week_start <= row_date < week_end):
                continue

            for data_key, total in totals.items():
                total["used"] += decimal_from_any(row.get(f"{data_key}_cost"))
                total["requests"] += int_from_any(row.get(f"{data_key}_calls"))

    ranked = list(totals.values())
    ranked.sort(key=lambda item: (-item["used"], -item["requests"], item["name"].casefold()))

    return [
        {
            "rank": index + 1,
            "userId": item["userId"],
            "name": item["name"],
            "usedUsd": money_float(item["used"]),
            "requestCount": item["requests"],
            "apiKeyHashes": item["apiKeyHashes"],
        }
        for index, item in enumerate(ranked)
    ]


def statistics_users_from_datasets(datasets: list[Any]) -> list[dict[str, Any]]:
    users_by_key: dict[str, dict[str, Any]] = {}
    for dataset in datasets:
        if not isinstance(dataset, dict):
            continue
        users = dataset.get("users")
        if not isinstance(users, list):
            continue
        for user in users:
            if not isinstance(user, dict):
                continue
            data_key = user.get("dataKey")
            if not isinstance(data_key, str) or not data_key:
                continue
            name = user.get("name")
            user_id = user.get("id")
            parsed_user_id = int_from_any(user_id)
            users_by_key[data_key] = {
                "dataKey": data_key,
                "name": str(name) if name else "Unknown",
                "id": parsed_user_id if parsed_user_id > 0 else None,
            }
    return list(users_by_key.values())


def extract_api_key_hashes(data: Any) -> list[str]:
    if not isinstance(data, list):
        return []

    key_hashes: set[str] = set()
    for key in data:
        if not isinstance(key, dict):
            continue
        api_key = key.get("key")
        if isinstance(api_key, str) and api_key:
            key_hashes.add(api_key_hash(api_key))

    return sorted(key_hashes)


def public_leaderboard_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in entry.items()
        if key != "apiKeyHashes"
    }


def active_leaderboard_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        entry
        for entry in entries
        if decimal_from_any(entry.get("usedUsd")) > 0
        or int_from_any(entry.get("requestCount")) > 0
    ]


def api_key_hash(api_key: str) -> str:
    return sha256(api_key.encode("utf-8")).hexdigest()
