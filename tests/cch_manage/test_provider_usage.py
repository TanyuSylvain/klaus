import asyncio
from pathlib import Path

from cch_manage.coding_plan import TIER_FIVE_HOUR, TIER_WEEKLY_LIMIT, UsageTier
from cch_manage.config import (
    UsageMonitorConfig,
    UsageMonitorKeyConfig,
    UsageQueryConfig,
    usage_monitor_key_id,
)
from cch_manage.provider_usage import (
    PUBLIC_ERROR_MESSAGE,
    ProviderUsageManager,
    UsageSnapshot,
    adaptive_interval_seconds,
    apply_provider_manage,
    average_snapshots,
    filter_providers_by_names,
    should_disable_providers,
    should_enable_providers,
    status_slugs_for_provider_group,
)
from cch_manage.status_page import inject_status_script


def _key(
    api_key: str = "k",
    names: tuple[str, ...] = (),
    weight: float = 1.0,
) -> UsageMonitorKeyConfig:
    return UsageMonitorKeyConfig.from_api_key(api_key, names, weight)


def _manager(tmp_path: Path, keys: tuple[UsageMonitorKeyConfig, ...] | None = None):
    monitors = [
        UsageMonitorConfig(
            provider_group="claude",
            base_url="https://open.bigmodel.cn",
            provider="glm",
            keys=keys or (_key("k"),),
        )
    ]
    query = UsageQueryConfig(state_path=str(tmp_path / "state.json"))
    return ProviderUsageManager(monitors, query)


def _state(manager: ProviderUsageManager, key: UsageMonitorKeyConfig | None = None):
    monitor = manager.monitors[0]
    resolved = key or monitor.keys[0]
    return manager._runtime[(monitor.provider_group, resolved.key_id)]


def test_adaptive_interval_linear():
    # base=300, min=30, high=90
    assert adaptive_interval_seconds(0, base_interval_seconds=300, min_interval_seconds=30) == 300
    assert adaptive_interval_seconds(89.9, base_interval_seconds=300, min_interval_seconds=30) == 300
    assert adaptive_interval_seconds(90, base_interval_seconds=300, min_interval_seconds=30) == 300
    mid = adaptive_interval_seconds(95, base_interval_seconds=300, min_interval_seconds=30)
    assert mid == 165.0
    assert adaptive_interval_seconds(100, base_interval_seconds=300, min_interval_seconds=30) == 30
    assert adaptive_interval_seconds(120, base_interval_seconds=300, min_interval_seconds=30) == 30


def test_should_enable_prefers_force_disabled():
    last = UsageSnapshot(success=True, exhausted=False, tiers=[])
    assert should_enable_providers(force_disabled=True, last=last, current_exhausted=False) is True
    assert should_enable_providers(force_disabled=False, last=last, current_exhausted=False) is False


def test_should_enable_fallback_to_last_snapshot():
    last_exhausted = UsageSnapshot(
        success=True,
        exhausted=True,
        tiers=[UsageTier(TIER_FIVE_HOUR, 100.0)],
    )
    last_ok = UsageSnapshot(
        success=True,
        exhausted=False,
        tiers=[UsageTier(TIER_FIVE_HOUR, 40.0)],
    )
    assert (
        should_enable_providers(force_disabled=None, last=last_exhausted, current_exhausted=False)
        is True
    )
    assert (
        should_enable_providers(force_disabled=None, last=last_ok, current_exhausted=False) is False
    )
    assert (
        should_enable_providers(force_disabled=None, last=None, current_exhausted=False) is False
    )
    assert (
        should_enable_providers(
            force_disabled=None, last=last_exhausted, current_exhausted=True
        )
        is False
    )


def test_should_disable_on_current_exhausted():
    assert should_disable_providers(current_exhausted=True) is True
    assert should_disable_providers(current_exhausted=False) is False


def test_status_slugs_for_provider_group():
    mapping = {"opus": ["claude"], "sonnet": ["claude", "other"], "mini": ["minimax"]}
    assert status_slugs_for_provider_group("claude", mapping) == ["opus", "sonnet"]
    assert status_slugs_for_provider_group("minimax", mapping) == ["mini"]
    assert status_slugs_for_provider_group("kimi", mapping) == ["kimi"]
    assert status_slugs_for_provider_group("kimi", None) == ["kimi"]


def test_apply_provider_manage_disable_and_enable():
    providers = [
        {"id": 1, "is_enabled": True},
        {"id": 2, "is_enabled": False},
        {"id": 3, "isEnabled": True},
    ]
    calls: list[tuple[int, bool]] = []

    async def set_enabled(provider_id: int, enabled: bool) -> bool:
        calls.append((provider_id, enabled))
        return True

    exhausted = UsageSnapshot(
        success=True,
        exhausted=True,
        max_utilization=100.0,
        tiers=[UsageTier(TIER_FIVE_HOUR, 100.0), UsageTier(TIER_WEEKLY_LIMIT, 50.0)],
    )
    ok = UsageSnapshot(
        success=True,
        exhausted=False,
        max_utilization=40.0,
        tiers=[UsageTier(TIER_FIVE_HOUR, 40.0)],
    )

    async def run():
        flag, result = await apply_provider_manage(
            providers,
            force_disabled=None,
            last=None,
            current=exhausted,
            set_provider_enabled=set_enabled,
        )
        assert flag is True
        assert result["action"] == "disable"
        assert set(result["disabledProviderIds"]) == {1, 3}

        # After disable, providers list would show disabled; simulate reset with force flag.
        disabled_providers = [
            {"id": 1, "is_enabled": False},
            {"id": 2, "is_enabled": False},
            {"id": 3, "is_enabled": False},
        ]
        flag2, result2 = await apply_provider_manage(
            disabled_providers,
            force_disabled=True,
            last=exhausted,
            current=ok,
            set_provider_enabled=set_enabled,
        )
        assert flag2 is False
        assert result2["action"] == "enable"
        assert set(result2["enabledProviderIds"]) == {1, 2, 3}

        # Fallback: force_disabled missing, last exhausted.
        flag3, result3 = await apply_provider_manage(
            disabled_providers,
            force_disabled=None,
            last=exhausted,
            current=ok,
            set_provider_enabled=set_enabled,
        )
        assert flag3 is False
        assert result3["action"] == "enable"

        # Failed query: no change.
        failed = UsageSnapshot(success=False, error="boom")
        flag4, result4 = await apply_provider_manage(
            providers,
            force_disabled=True,
            last=exhausted,
            current=failed,
            set_provider_enabled=set_enabled,
        )
        assert flag4 is True
        assert result4["action"] == "none"

    asyncio.run(run())


def test_public_payload_falls_back_to_last_good_snapshot(tmp_path: Path):
    manager = _manager(tmp_path)
    state = _state(manager)
    state.last = UsageSnapshot(
        success=True,
        max_utilization=42.0,
        tiers=[UsageTier(TIER_FIVE_HOUR, 42.0)],
    )
    state.current = UsageSnapshot(success=False, error="Network error: boom")

    group = manager.public_payload()["groups"][0]
    assert group["success"] is True
    assert group["stale"] is True
    assert group["tiers"] == [{"name": TIER_FIVE_HOUR, "utilization": 42.0, "resetsAt": None}]
    assert group["error"] is None
    assert group["keyUsages"] == [
        {
            "success": True,
            "stale": True,
            "tiers": [{"name": TIER_FIVE_HOUR, "utilization": 42.0, "resetsAt": None}],
            "error": None,
        }
    ]


def test_public_payload_hides_vendor_error_detail(tmp_path: Path):
    manager = _manager(tmp_path)
    state = _state(manager)
    state.current = UsageSnapshot(
        success=False,
        error="API error (HTTP 500): {'account': 'secret-detail'}",
    )

    group = manager.public_payload()["groups"][0]
    assert group["success"] is False
    assert group["stale"] is False
    assert group["error"] == PUBLIC_ERROR_MESSAGE
    assert group["keyUsages"] == [
        {"success": False, "stale": False, "tiers": [], "error": PUBLIC_ERROR_MESSAGE}
    ]
    assert "secret-detail" not in str(group)
    assert "autoManage" not in group


def test_persist_roundtrip(tmp_path: Path):
    state_path = tmp_path / "state.json"
    key = _key("k")
    monitors = [
        UsageMonitorConfig(
            provider_group="claude",
            base_url="https://open.bigmodel.cn",
            provider="glm",
            keys=(key,),
        )
    ]
    query = UsageQueryConfig(state_path=str(state_path))
    manager = ProviderUsageManager(monitors, query)
    state = manager._runtime[("claude", key.key_id)]
    state.force_disabled = True
    state.last = UsageSnapshot(
        success=True,
        exhausted=True,
        max_utilization=100.0,
        tiers=[UsageTier(TIER_FIVE_HOUR, 100.0)],
        queried_at="2026-01-01T00:00:00+00:00",
    )
    manager._persist_state()

    manager2 = ProviderUsageManager(monitors, query)
    state2 = manager2._runtime[("claude", key.key_id)]
    assert state2.force_disabled is True
    assert state2.last is not None
    assert state2.last.exhausted is True
    assert state2.last.tiers[0].utilization == 100.0
    saved = state_path.read_text(encoding="utf-8")
    assert "keys" in saved
    assert key.key_id in saved
    assert '"k"' not in saved


def test_average_snapshots_per_tier_and_earliest_reset():
    first = UsageSnapshot(
        success=True,
        max_utilization=80.0,
        plan_label="max",
        queried_at="2026-01-01T01:00:00+00:00",
        tiers=[
            UsageTier(TIER_FIVE_HOUR, 80.0, resets_at="2026-01-01T12:00:00+00:00"),
            UsageTier(TIER_WEEKLY_LIMIT, 20.0, resets_at="2026-01-07T00:00:00+00:00"),
        ],
    )
    second = UsageSnapshot(
        success=True,
        max_utilization=40.0,
        plan_label="max",
        queried_at="2026-01-01T02:00:00Z",
        tiers=[
            UsageTier(TIER_FIVE_HOUR, 40.0, resets_at="2026-01-01T10:00:00Z"),
            UsageTier(TIER_WEEKLY_LIMIT, 60.0, resets_at="2026-01-08T00:00:00+00:00"),
        ],
    )
    averaged = average_snapshots([first, second], disable_threshold_pct=100.0)
    assert averaged is not None
    assert averaged.success is True
    assert averaged.plan_label == "max"
    assert averaged.queried_at == "2026-01-01T02:00:00Z"
    by_name = {tier.name: tier for tier in averaged.tiers}
    assert by_name[TIER_FIVE_HOUR].utilization == 60.0
    assert by_name[TIER_FIVE_HOUR].resets_at == "2026-01-01T10:00:00Z"
    assert by_name[TIER_WEEKLY_LIMIT].utilization == 40.0
    assert averaged.max_utilization == 60.0
    assert averaged.exhausted is False


def test_average_snapshots_skips_failed_and_mixed_plan_label():
    ok = UsageSnapshot(
        success=True,
        max_utilization=10.0,
        plan_label="pro",
        tiers=[UsageTier(TIER_FIVE_HOUR, 10.0)],
    )
    other = UsageSnapshot(
        success=True,
        max_utilization=30.0,
        plan_label="max",
        tiers=[UsageTier(TIER_FIVE_HOUR, 30.0)],
    )
    failed = UsageSnapshot(success=False, error="boom")
    averaged = average_snapshots([ok, failed, other], disable_threshold_pct=100.0)
    assert averaged is not None
    assert averaged.tiers[0].utilization == 20.0
    assert averaged.plan_label is None
    assert average_snapshots([failed], disable_threshold_pct=100.0) is None


def test_average_snapshots_weighted_mean():
    heavy = UsageSnapshot(
        success=True,
        max_utilization=80.0,
        tiers=[
            UsageTier(TIER_FIVE_HOUR, 80.0),
            UsageTier(TIER_WEEKLY_LIMIT, 20.0),
        ],
    )
    light = UsageSnapshot(
        success=True,
        max_utilization=40.0,
        tiers=[
            UsageTier(TIER_FIVE_HOUR, 40.0),
            UsageTier(TIER_WEEKLY_LIMIT, 60.0),
        ],
    )
    averaged = average_snapshots(
        [heavy, light],
        disable_threshold_pct=100.0,
        weights=[3.0, 1.0],
    )
    assert averaged is not None
    by_name = {tier.name: tier for tier in averaged.tiers}
    assert by_name[TIER_FIVE_HOUR].utilization == 70.0
    assert by_name[TIER_WEEKLY_LIMIT].utilization == 30.0
    assert averaged.max_utilization == 70.0


def test_filter_providers_by_names():
    providers = [
        {"id": 1, "name": "glm-a"},
        {"id": 2, "providerName": "glm-b"},
        {"id": 3, "name": "glm-c"},
    ]
    assert [p["id"] for p in filter_providers_by_names(providers, ())] == [1, 2, 3]
    assert [p["id"] for p in filter_providers_by_names(providers, ("glm-a", "glm-b"))] == [1, 2]


def test_public_payload_averages_keys_and_marks_stale(tmp_path: Path):
    key_a = _key("key-a", ("glm-a",), weight=3.0)
    key_b = _key("key-b", ("glm-b",), weight=1.0)
    manager = _manager(tmp_path, keys=(key_a, key_b))
    state_a = _state(manager, key_a)
    state_b = _state(manager, key_b)
    state_a.current = UsageSnapshot(
        success=True,
        max_utilization=80.0,
        tiers=[
            UsageTier(TIER_FIVE_HOUR, 80.0),
            UsageTier(TIER_WEEKLY_LIMIT, 20.0),
        ],
    )
    state_b.last = UsageSnapshot(
        success=True,
        max_utilization=40.0,
        tiers=[
            UsageTier(TIER_FIVE_HOUR, 40.0),
            UsageTier(TIER_WEEKLY_LIMIT, 60.0),
        ],
    )
    state_b.current = UsageSnapshot(success=False, error="API error (HTTP 500): secret")

    group = manager.public_payload()["groups"][0]
    assert group["success"] is True
    assert group["stale"] is True
    by_name = {tier["name"]: tier for tier in group["tiers"]}
    # (80*3 + 40*1) / 4 = 70; (20*3 + 60*1) / 4 = 30
    assert by_name[TIER_FIVE_HOUR]["utilization"] == 70.0
    assert by_name[TIER_WEEKLY_LIMIT]["utilization"] == 30.0
    assert group["maxUtilization"] == 70.0
    assert group["error"] is None
    assert "secret" not in str(group)
    assert "key-a" not in str(group)
    assert "glm-a" not in str(group)
    assert usage_monitor_key_id("key-a") not in str(group)
    assert usage_monitor_key_id("key-b") not in str(group)
    key_usages = group["keyUsages"]
    assert len(key_usages) == 2
    assert set(key_usages[0]) == {"success", "stale", "tiers", "error"}
    assert key_usages[0]["success"] is True
    assert key_usages[0]["stale"] is False
    assert key_usages[0]["tiers"][0]["utilization"] == 80.0
    assert key_usages[1]["success"] is True
    assert key_usages[1]["stale"] is True
    assert key_usages[1]["tiers"][0]["utilization"] == 40.0


def test_inject_status_script_has_usage_key_tooltip():
    html = inject_status_script("<body></body>", ["opus"], 300)
    assert "cch-provider-usage-tooltip" in html
    assert "keyUsages" in html
    assert "showUsageTooltip" in html
    assert "onmouseenter" in html


def test_legacy_state_migrates_onto_single_key(tmp_path: Path):
    import json

    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "claude": {
                    "forceDisabled": True,
                    "last": {
                        "success": True,
                        "exhausted": True,
                        "maxUtilization": 100.0,
                        "tiers": [{"name": TIER_FIVE_HOUR, "utilization": 100.0}],
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    manager = _manager(tmp_path)
    state = _state(manager)
    assert state.force_disabled is True
    assert state.last is not None
    assert state.last.exhausted is True


def test_poll_manages_only_named_providers(tmp_path: Path, monkeypatch):
    from cch_manage.coding_plan import CodingPlanQuota
    from cch_manage import provider_usage as provider_usage_mod

    key_a = _key("key-a", ("glm-a",))
    key_b = _key("key-b", ("glm-b", "glm-c"))
    manager = _manager(tmp_path, keys=(key_a, key_b))

    async def fake_query(client, *, provider, base_url, api_key, timeout_seconds=15.0):
        util = 100.0 if api_key == "key-a" else 10.0
        return CodingPlanQuota(
            success=True,
            provider="glm",
            tiers=[
                UsageTier(TIER_FIVE_HOUR, util),
                UsageTier(TIER_WEEKLY_LIMIT, 20.0),
            ],
        )

    monkeypatch.setattr(provider_usage_mod, "query_coding_plan_quota", fake_query)

    providers = [
        {"id": 1, "name": "glm-a", "is_enabled": True},
        {"id": 2, "name": "glm-b", "is_enabled": True},
        {"id": 3, "name": "glm-c", "is_enabled": True},
    ]
    calls: list[tuple[int, bool]] = []

    async def set_enabled(provider_id: int, enabled: bool) -> bool:
        calls.append((provider_id, enabled))
        return True

    async def get_group(_group: str):
        return providers

    async def run():
        await manager.poll_due(
            None,  # type: ignore[arg-type]
            get_group_providers=get_group,
            set_provider_enabled=set_enabled,
            force=True,
        )

    asyncio.run(run())
    assert (1, False) in calls
    assert (2, False) not in calls
    assert (3, False) not in calls
    assert _state(manager, key_a).force_disabled is True
    assert _state(manager, key_b).force_disabled is not True
