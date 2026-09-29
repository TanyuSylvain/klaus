import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from cch_manage.config import CchStatusConfig
from cch_manage.feature import CchStatusFeature
from cch_manage.rankings import (
    RankSnapshotMeta,
    StoredRankRow,
    build_rank_response,
    sync_rankings,
)
from cch_manage.status_page import inject_status_script


TZ = ZoneInfo("Asia/Shanghai")
WEEK_START = datetime(2026, 9, 28, 0, 0, tzinfo=TZ)
WEEK_END = WEEK_START + timedelta(days=7)
QUERY_END = datetime(2026, 9, 29, 18, 0, tzinfo=TZ)
UPDATED_AT = datetime(2026, 9, 29, 18, 5, tzinfo=TZ)


class _Store:
    def __init__(self) -> None:
        self.calls = 0
        self.rows: list[StoredRankRow] = []
        self.meta: RankSnapshotMeta | None = None

    async def replace_all(
        self,
        rows: list[StoredRankRow],
        *,
        week_start: datetime,
        week_end: datetime,
        updated_at: datetime,
    ) -> None:
        self.calls += 1
        self.rows = list(rows)
        self.meta = RankSnapshotMeta(week_start, week_end, updated_at)

    async def list_snapshot(self) -> tuple[list[StoredRankRow], RankSnapshotMeta | None]:
        return list(self.rows), self.meta


class _Client:
    def __init__(self, stats: dict[tuple[str, str], dict[str, Any]], *, fail: bool = False) -> None:
        self.stats = stats
        self.fail = fail
        self.calls: list[dict[str, Any]] = []
        self.list_calls = 0

    async def list_users(self) -> list[dict[str, Any]]:
        self.list_calls += 1
        return [
            {"id": 1, "name": "ada"},
            {"id": 2, "name": " ada "},
            {"id": True, "name": "ignored"},
            {"id": 3, "name": "  "},
        ]

    async def list_usage_models(self) -> list[str]:
        return ["glm-5.3", "k3", ""]

    async def get_usage_log_stats(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("CCH down")
        if kwargs.get("user_id") is not None:
            return self.stats[("user", str(kwargs["user_id"]))]
        return self.stats[("model", str(kwargs.get("model")))]


def _stats() -> dict[tuple[str, str], dict[str, Any]]:
    return {
        ("user", "1"): {"totalRequests": 2, "totalTokens": 50, "totalCost": "1.5"},
        ("user", "2"): {"totalRequests": 9, "totalTokens": 10, "totalCost": "1.5"},
        ("user", "3"): {"totalRequests": 4, "totalTokens": 8, "totalCost": "0.25"},
        ("model", "glm-5.3"): {"totalRequests": 0, "totalTokens": 0, "totalCost": 0},
        ("model", "k3"): {"totalRequests": 3, "totalTokens": 80, "totalCost": "0.4"},
    }


def test_sync_rankings_maps_totals_drops_zeros_and_keeps_duplicate_names():
    client = _Client(_stats())
    store = _Store()

    count = asyncio.run(
        sync_rankings(
            client,
            store,
            week_start=WEEK_START,
            week_end=WEEK_END,
            query_end=QUERY_END,
            updated_at=UPDATED_AT,
        )
    )

    assert count == 4
    assert store.calls == 1
    assert store.meta is not None
    assert store.meta.week_start == WEEK_START
    assert store.meta.week_end == WEEK_END
    assert [(row.scope, row.entity_key, row.name) for row in store.rows] == [
        ("user", "1", "ada"),
        ("user", "2", "ada"),
        ("model", "k3", "k3"),
        ("user", "3", "用户 3"),
    ]
    assert store.rows[0].request_count == 2
    assert store.rows[0].token_count == 50
    assert store.rows[0].cost_usd == Decimal("1.5")
    assert {call["start_time_ms"] for call in client.calls} == {int(WEEK_START.timestamp() * 1000)}
    assert {call["end_time_ms"] for call in client.calls} == {int(QUERY_END.timestamp() * 1000)}
    assert ("model", "glm-5.3") not in {(row.scope, row.entity_key) for row in store.rows}


def test_failed_rank_collection_does_not_replace_the_snapshot():
    client = _Client(_stats(), fail=True)
    store = _Store()

    with pytest.raises(RuntimeError, match="rank refresh failed"):
        asyncio.run(
            sync_rankings(
                client,
                store,
                week_start=WEEK_START,
                week_end=WEEK_END,
                query_end=QUERY_END,
                updated_at=UPDATED_AT,
            )
        )

    assert store.calls == 0


def test_rank_response_uses_the_snapshot_clock_and_sorts_each_side():
    rows = [
        StoredRankRow("user", "2", "beau", 1, 1, Decimal("0.10")),
        StoredRankRow("user", "1", "ada", 9, 9, Decimal("2")),
        StoredRankRow("model", "k3", "k3", 3, 80, Decimal("0.40")),
    ]
    empty = build_rank_response(rows, None, generated_at="now")
    assert empty["syncedAt"] is None
    assert empty["users"] == []

    payload = build_rank_response(
        rows,
        RankSnapshotMeta(WEEK_START, WEEK_END, UPDATED_AT),
        generated_at="now",
    )
    assert payload["syncedAt"] == UPDATED_AT.isoformat()
    assert payload["weekStart"] == WEEK_START.isoformat()
    assert payload["weekEnd"] == WEEK_END.isoformat()
    assert [row["name"] for row in payload["users"]] == ["ada", "beau"]
    assert payload["users"][0]["costUsd"] == 2.0
    assert payload["models"][0]["tokenCount"] == 80


def test_rankings_config_parses_defaults_and_rejects_a_short_interval():
    cfg = CchStatusConfig.from_dict(
        {
            "enabled": True,
            "base_url": "http://cch.local",
            "admin_token": "token",
            "rankings": {"database_url": "postgresql://llm_proxy@127.0.0.1:5432/llm_proxy"},
        }
    )
    assert cfg is not None
    assert cfg.rankings is not None
    assert cfg.rankings.database_url == "postgresql://llm_proxy@127.0.0.1:5432/llm_proxy"
    assert cfg.rankings.update_interval_seconds == 21600

    omitted = CchStatusConfig.from_dict(
        {"enabled": True, "base_url": "http://cch.local", "admin_token": "token"}
    )
    assert omitted is not None
    assert omitted.rankings is None

    with pytest.raises(ValueError, match="rankings.update_interval_seconds"):
        CchStatusConfig.from_dict(
            {
                "enabled": True,
                "base_url": "http://cch.local",
                "admin_token": "token",
                "rankings": {
                    "database_url": "postgresql://llm_proxy@127.0.0.1/llm_proxy",
                    "update_interval_seconds": False,
                },
            }
        )


def test_rankings_endpoint_reads_the_store_without_calling_cch():
    feature = CchStatusFeature(CchStatusConfig(base_url="http://cch.local", admin_token="token"))
    store = _Store()
    store.rows = [StoredRankRow("model", "k3", "k3", 3, 80, Decimal("0.4"))]
    store.meta = RankSnapshotMeta(WEEK_START, WEEK_END, UPDATED_AT)

    class Cch:
        def __init__(self) -> None:
            self.calls = 0

        async def list_users(self) -> list[dict[str, Any]]:
            self.calls += 1
            return []

        async def list_usage_models(self) -> list[str]:
            self.calls += 1
            return []

        async def get_usage_log_stats(self, **kwargs: object) -> dict[str, Any]:
            self.calls += 1
            return {}

    cch = Cch()
    feature._rank_store = store
    feature._cch = cch

    payload = asyncio.run(feature.get_rankings())

    assert cch.calls == 0
    assert payload["models"][0]["name"] == "k3"
    assert payload["models"][0]["requestCount"] == 3
    assert payload["weekStart"] == WEEK_START.isoformat()
    assert payload["users"] == []


def test_status_script_includes_the_rank_table_control():
    html = inject_status_script("<body></body>", ["lux"], 300)

    assert html.find('toggle.textContent = "模型价格表"') < html.find('toggle.textContent = "排行榜"')
    assert "/cch/status/rankings" in html
    assert 'data-rank-side="user"' in html
    assert 'data-rank-side="model"' in html
    assert ">用户</span>" in html
    assert ">模型</span>" in html
    assert "请求数" in html
    assert "Token数" in html
    assert "tokensM(row.tokenCount)" in html
    assert "消耗金额" in html
    assert "排行榜暂时不可用" in html
    assert 'panel.style.gridRow = "3"' in html
    assert "minmax(0, 6fr) minmax(0, 4fr)" in html
