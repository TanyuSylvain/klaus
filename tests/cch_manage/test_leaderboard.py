import asyncio
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from cch_manage.config import CchStatusConfig
from cch_manage.feature import CchStatusFeature
from cch_manage.leaderboard import (
    api_key_hash,
    extract_api_key_hashes,
    get_weekly_leaderboard,
    restrict_leaderboard,
)
from cch_manage.user_tags import user_matches_tags

TZ = ZoneInfo("Asia/Shanghai")

# Monday 2026-08-17 00:00 +08:00 .. Monday 2026-08-24 00:00 +08:00
WEEK_START = datetime(2026, 8, 17, 0, 0, 0, tzinfo=TZ)
WEEK_END = datetime(2026, 8, 24, 0, 0, 0, tzinfo=TZ)


class _FakeCch:
    def __init__(
        self,
        statistics: dict[str, Any],
        keys_by_user: dict[int, list[dict[str, Any]]],
    ) -> None:
        self._statistics = statistics
        self._keys_by_user = keys_by_user
        self.revealed: list[int] = []
        self.requested_statistics: list[str] = []

    async def get_dashboard_statistics(self, time_range: str = "7days") -> dict[str, Any]:
        self.requested_statistics.append(time_range)
        return self._statistics

    async def list_user_keys(self, user_id: int) -> list[dict[str, Any]]:
        return self._keys_by_user.get(user_id, [])

    async def reveal_key(self, key_id: int) -> str:
        self.revealed.append(key_id)
        return f"sk-{key_id}"


def _statistics() -> dict[str, Any]:
    return {
        "timeRange": "7days",
        "users": [
            {"id": 1, "name": "alice", "dataKey": "user-1"},
            {"id": 2, "name": "bob", "dataKey": "user-2"},
        ],
        "chartData": [
            # In-week rows (2026-08-18 and 2026-08-19 local time).
            {
                "date": "2026-08-17T16:00:00.000Z",
                "user-1_cost": "5.000000000000000",
                "user-1_calls": 10,
                "user-2_cost": "3.000000000000000",
                "user-2_calls": 99,
            },
            {
                "date": "2026-08-18T16:00:00.000Z",
                "user-1_cost": "1.000000000000000",
                "user-1_calls": 2,
                "user-2_cost": "0.000000000000000",
                "user-2_calls": 0,
            },
            # Before the week: ignored even though the cost is large.
            {
                "date": "2026-08-15T16:00:00.000Z",
                "user-1_cost": "100.000000000000000",
                "user-1_calls": 1000,
                "user-2_cost": "0.000000000000000",
                "user-2_calls": 0,
            },
            # Exactly the week end: excluded (half-open range).
            {
                "date": "2026-08-23T16:00:00.000Z",
                "user-1_cost": "50.000000000000000",
                "user-1_calls": 500,
                "user-2_cost": "0.000000000000000",
                "user-2_calls": 0,
            },
        ],
    }


def _keys_by_user() -> dict[int, list[dict[str, Any]]]:
    return {
        # Disabled keys are still hashed (old getKeys behavior).
        1: [
            {"id": 11, "isEnabled": False, "deletedAt": None},
            {"id": 12, "isEnabled": True, "deletedAt": None},
        ],
        # Soft-deleted keys are skipped.
        2: [
            {"id": 21, "isEnabled": True, "deletedAt": "2026-01-01T00:00:00.000Z"},
            {"id": 22, "isEnabled": True, "deletedAt": None},
        ],
    }


def test_get_weekly_leaderboard_ranks_filters_and_hashes():
    cch = _FakeCch(_statistics(), _keys_by_user())

    async def run() -> list[dict[str, Any]]:
        return await get_weekly_leaderboard(cch, WEEK_START, WEEK_END, TZ)

    leaderboard = asyncio.run(run())

    assert cch.requested_statistics == ["7days"]
    assert cch.revealed == [11, 12, 22]
    assert [entry["name"] for entry in leaderboard] == ["alice", "bob"]
    assert [entry["rank"] for entry in leaderboard] == [1, 2]

    alice = leaderboard[0]
    assert alice["usedUsd"] == 6.0
    assert alice["requestCount"] == 12
    assert alice["apiKeyHashes"] == sorted([api_key_hash("sk-11"), api_key_hash("sk-12")])

    bob = leaderboard[1]
    assert bob["usedUsd"] == 3.0
    assert bob["requestCount"] == 99
    assert bob["apiKeyHashes"] == [api_key_hash("sk-22")]


def test_get_weekly_leaderboard_skips_users_without_id():
    statistics = _statistics()
    statistics["users"] = statistics["users"] + [
        {"id": None, "name": "ghost", "dataKey": "user-3"}
    ]

    cch = _FakeCch(statistics, _keys_by_user())

    async def run() -> list[dict[str, Any]]:
        return await get_weekly_leaderboard(cch, WEEK_START, WEEK_END, TZ)

    leaderboard = asyncio.run(run())
    assert cch.revealed == [11, 12, 22]
    # ghost has no id: no key lookups, but still ranked from chart rows.
    assert [entry["name"] for entry in leaderboard] == ["alice", "bob", "ghost"]
    ghost = leaderboard[2]
    assert ghost["apiKeyHashes"] == []
    assert ghost["usedUsd"] == 0.0


def test_restrict_leaderboard_renumbers_the_tagged_subset():
    entries = [
        {"rank": 1, "userId": 2, "name": "beau", "usedUsd": 9.0, "requestCount": 3},
        {"rank": 2, "userId": 1, "name": "ada", "usedUsd": 1.0, "requestCount": 4},
        {"rank": 3, "userId": None, "name": "ghost", "usedUsd": 0.5, "requestCount": 1},
    ]

    visible = restrict_leaderboard(entries, {1})

    assert [(entry["name"], entry["rank"]) for entry in visible] == [("ada", 1)]
    assert entries[0]["rank"] == 1
    assert user_matches_tags({"tags": ["vip", "internal"]}, ("external", "internal"))
    assert not user_matches_tags({"tags": ["external"]}, ("internal",))
    assert user_matches_tags({}, ())


def test_public_leaderboard_uses_configured_tags_and_keeps_usage_lookup():
    tz = ZoneInfo("Asia/Shanghai")
    today = datetime.now(tz).isoformat()

    class FakeCch:
        async def get_dashboard_statistics(self, time_range: str = "7days") -> dict[str, Any]:
            assert time_range == "7days"
            return {
                "users": [
                    {"id": 1, "name": "ada", "dataKey": "user-1"},
                    {"id": 2, "name": "beau", "dataKey": "user-2"},
                ],
                "chartData": [
                    {
                        "date": today,
                        "user-1_cost": "1.000000000000000",
                        "user-1_calls": 4,
                        "user-2_cost": "9.000000000000000",
                        "user-2_calls": 3,
                    }
                ],
            }

        async def list_user_keys(self, user_id: int) -> list[dict[str, Any]]:
            return []

        async def list_users(self) -> list[dict[str, Any]]:
            return [
                {"id": 1, "name": "ada", "tags": ["internal"]},
                {"id": 2, "name": "beau", "tags": ["external"]},
            ]

    feature = CchStatusFeature(
        CchStatusConfig(
            base_url="http://cch.local",
            admin_token="token",
            weekly_leaderboard_enabled=True,
            user_tags=("internal",),
        )
    )
    feature._cch = FakeCch()

    payload = asyncio.run(feature.get_weekly_leaderboard())

    assert [(entry["name"], entry["rank"]) for entry in payload["leaderboard"]] == [("ada", 1)]
    assert payload["weeklyTotalUsedUsd"] == 1.0
    assert payload["weeklyTotalRequests"] == 4
    assert [(entry["name"], entry["rank"]) for entry in payload["userUsage"]] == [
        ("beau", 1),
        ("ada", 2),
    ]


def test_extract_api_key_hashes_dedupes_and_sorts():
    hashes = extract_api_key_hashes(["sk-b", "sk-a", "sk-b", "", None])
    assert hashes == sorted([api_key_hash("sk-a"), api_key_hash("sk-b")])
