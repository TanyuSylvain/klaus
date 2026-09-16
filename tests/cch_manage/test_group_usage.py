import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from cch_manage.group_usage import (
    build_group_usage_payload,
    collect_provider_usage_stats,
    parse_usage_log_stats,
    provider_ids_by_status_slug,
    sum_usage_stats,
)
from cch_manage.status_page import inject_status_script


TZ = ZoneInfo("Asia/Shanghai")
WEEK_START = datetime(2026, 9, 14, 0, 0, 0, tzinfo=TZ)
WEEK_END = datetime(2026, 9, 21, 0, 0, 0, tzinfo=TZ)
GENERATED_AT = datetime(2026, 9, 16, 12, 0, 0, tzinfo=TZ)


class _FakeCch:
    def __init__(
        self,
        stats: dict[int, dict[str, Any]],
        fail_ids: set[int] | None = None,
    ) -> None:
        self._stats = stats
        self.fail_ids = fail_ids or set()
        self.requested: list[int] = []

    async def get_usage_log_stats(
        self,
        *,
        provider_id: int,
        start_time_ms: int,
        end_time_ms: int,
    ) -> dict[str, Any]:
        self.requested.append(provider_id)
        if provider_id in self.fail_ids:
            raise RuntimeError("stats unavailable")
        return self._stats[provider_id]


def _providers() -> list[dict[str, Any]]:
    return [
        {"id": 1, "name": "claude-a", "groupTag": "claude", "isEnabled": True},
        {"id": 2, "name": "claude-b", "groupTag": "claude", "isEnabled": False},
        {"id": 3, "name": "glm-a", "groupTag": "glm", "isEnabled": True},
        {"id": 4, "name": "kimi-a", "groupTag": "kimi", "isEnabled": True},
        {"id": 5, "name": "qwen-a", "groupTag": "qwen", "isEnabled": True},
    ]


def _public_groups() -> list[dict[str, Any]]:
    return [
        {"publicGroupSlug": "opus", "displayName": "claude-opus"},
        {"publicGroupSlug": "glm", "displayName": "glm"},
        {"publicGroupSlug": "kimi", "displayName": "kimi"},
    ]


def test_provider_ids_by_status_slug_maps_opus_and_includes_disabled():
    ids = provider_ids_by_status_slug(
        _providers(),
        _public_groups(),
        {"opus": ["claude"]},
    )
    assert ids["opus"] == [1, 2]
    assert ids["glm"] == [3]
    assert ids["kimi"] == [4]
    assert "qwen" not in ids


def test_provider_ids_by_status_slug_defaults_slug_to_provider_group():
    ids = provider_ids_by_status_slug(_providers(), _public_groups(), None)
    assert "opus" not in ids
    assert ids["glm"] == [3]


def test_parse_and_sum_usage_stats():
    parsed = parse_usage_log_stats(
        {"totalRequests": 10, "totalCost": "1.255", "totalTokens": 2_500_000}
    )
    assert parsed == {
        "requestCount": 10,
        "totalTokens": 2_500_000,
        "cost": Decimal("1.255"),
    }
    assert parse_usage_log_stats({}) is None
    assert parse_usage_log_stats([1]) is None

    summed = sum_usage_stats(
        [
            {"requestCount": 10, "totalTokens": 1_000_000, "cost": "1.255"},
            {"requestCount": 3, "totalTokens": 500_000, "cost": "0.145"},
        ]
    )
    assert summed == {
        "requestCount": 13,
        "totalTokens": 1_500_000,
        "costUsd": 1.40,
    }


def test_build_group_usage_payload_sums_per_slug():
    payload = build_group_usage_payload(
        {"opus": [1, 2], "glm": [3]},
        {
            1: {"requestCount": 10, "totalTokens": 100, "cost": "1.10"},
            2: {"requestCount": 4, "totalTokens": 50, "cost": "0.40"},
            3: {"requestCount": 7, "totalTokens": 9, "cost": "0.01"},
        },
        WEEK_START,
        WEEK_END,
        GENERATED_AT,
    )
    assert payload["weekStart"] == WEEK_START.isoformat()
    assert payload["weekEnd"] == WEEK_END.isoformat()
    assert payload["generatedAt"] == GENERATED_AT.isoformat()
    assert payload["groups"]["opus"] == {
        "requestCount": 14,
        "totalTokens": 150,
        "costUsd": 1.50,
    }
    assert payload["groups"]["glm"]["requestCount"] == 7


def test_collect_provider_usage_stats_skips_failures():
    cch = _FakeCch(
        {
            1: {"totalRequests": 10, "totalCost": 1.0, "totalTokens": 100},
            2: {"totalRequests": 4, "totalCost": 0.4, "totalTokens": 50},
        },
        fail_ids={2},
    )

    async def run() -> dict[int, dict[str, Any]]:
        return await collect_provider_usage_stats(cch, [1, 2], 1000, 2000)

    stats = asyncio.run(run())
    assert sorted(cch.requested) == [1, 2]
    assert set(stats) == {1}
    assert stats[1]["requestCount"] == 10


def test_inject_status_script_group_usage_refresh_and_opus_omits_cost():
    html = inject_status_script("<body></body>", "opus", 300)
    assert "/cch/status/group-usage" in html
    assert "次" in html
    assert " M" in html
    assert "setInterval(loadGroupUsage, refreshMs)" in html
    assert "slug !== statusGroupSlug" in html
    assert html.count("Math.min(refreshMs, 30000)") == 1
    assert "pillStyle(usageBadgeColors)" in html
    assert "rgb(59 130 246 / .55)" in html
