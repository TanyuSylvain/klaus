import asyncio
from pathlib import Path

from cch_manage.coding_plan import TIER_FIVE_HOUR, TIER_WEEKLY_LIMIT, UsageTier
from cch_manage.provider_usage import (
    UsageSnapshot,
    adaptive_interval_seconds,
    apply_provider_manage,
    should_disable_providers,
    should_enable_providers,
    status_slugs_for_provider_group,
)


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


def _manager(tmp_path: Path):
    from cch_manage.config import UsageMonitorConfig, UsageQueryConfig
    from cch_manage.provider_usage import ProviderUsageManager

    monitors = [
        UsageMonitorConfig(
            provider_group="claude",
            base_url="https://open.bigmodel.cn",
            api_key="k",
            provider="glm",
        )
    ]
    query = UsageQueryConfig(state_path=str(tmp_path / "state.json"))
    return ProviderUsageManager(monitors, query)


def test_public_payload_falls_back_to_last_good_snapshot(tmp_path: Path):
    manager = _manager(tmp_path)
    state = manager._runtime["claude"]
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


def test_public_payload_hides_vendor_error_detail(tmp_path: Path):
    from cch_manage.provider_usage import PUBLIC_ERROR_MESSAGE

    manager = _manager(tmp_path)
    state = manager._runtime["claude"]
    state.current = UsageSnapshot(
        success=False,
        error="API error (HTTP 500): {'account': 'secret-detail'}",
    )

    group = manager.public_payload()["groups"][0]
    assert group["success"] is False
    assert group["stale"] is False
    assert group["error"] == PUBLIC_ERROR_MESSAGE
    assert "secret-detail" not in str(group)
    assert "autoManage" not in group


def test_persist_roundtrip(tmp_path: Path):
    from cch_manage.config import UsageMonitorConfig, UsageQueryConfig
    from cch_manage.provider_usage import ProviderUsageManager

    state_path = tmp_path / "state.json"
    monitors = [
        UsageMonitorConfig(
            provider_group="claude",
            base_url="https://open.bigmodel.cn",
            api_key="k",
            provider="glm",
        )
    ]
    query = UsageQueryConfig(state_path=str(state_path))
    manager = ProviderUsageManager(monitors, query)
    state = manager._runtime["claude"]
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
    state2 = manager2._runtime["claude"]
    assert state2.force_disabled is True
    assert state2.last is not None
    assert state2.last.exhausted is True
    assert state2.last.tiers[0].utilization == 100.0
