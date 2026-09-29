import asyncio
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from cch_manage.config import CchStatusConfig, QuotaGroupConfig
from cch_manage.feature import CchStatusFeature
from cch_manage.group_usage import provider_ids_by_status_slug
from cch_manage.quota import quota_usage_summary


ROOT = Path(__file__).resolve().parents[2]


def _enabled(base_url: str = "http://cch.local", **extra: object) -> dict:
    raw: dict = {"enabled": True, "base_url": base_url, "admin_token": "token"}
    raw.update(extra)
    return raw


def test_quota_groups_default_provider_group_and_thresholds():
    cfg = CchStatusConfig.from_dict(
        _enabled(
            quota_groups=[
                {"status_slug": "opus", "provider_group": "claude", "weekly_quota_usd": 200},
                {"status_slug": "glm", "weekly_quota_usd": "80", "auto_manage": True, "disable_at_pct": 90},
            ]
        )
    )
    assert cfg is not None
    assert cfg.quota_groups == (
        QuotaGroupConfig(
            status_slug="opus",
            provider_group="claude",
            weekly_quota_usd=Decimal("200"),
        ),
        QuotaGroupConfig(
            status_slug="glm",
            provider_group="glm",
            weekly_quota_usd=Decimal("80"),
            auto_manage=True,
            disable_at_pct=Decimal("90"),
        ),
    )
    assert cfg.card_provider_groups is None


def test_quota_group_links_its_card_without_a_separate_map():
    cfg = CchStatusConfig.from_dict(
        _enabled(
            card_provider_groups={"mini": ["minimax"]},
            quota_groups=[
                {
                    "status_slug": "lux",
                    "provider_group": "claude",
                    "weekly_quota_usd": 100,
                }
            ],
        )
    )
    assert cfg is not None
    assert cfg.card_provider_groups == {"mini": ["minimax"]}
    assert cfg.effective_card_provider_groups() == {
        "mini": ["minimax"],
        "lux": ["claude"],
    }
    ids = provider_ids_by_status_slug(
        [
            {"id": 1, "groupTag": "claude"},
            {"id": 2, "groupTag": "glm"},
        ],
        [{"publicGroupSlug": "lux"}, {"publicGroupSlug": "glm"}],
        cfg.effective_card_provider_groups(),
    )
    assert ids["lux"] == [1]
    assert ids["glm"] == [2]


def test_explicit_card_tags_keep_the_quota_provider_tag():
    cfg = CchStatusConfig.from_dict(
        _enabled(
            card_provider_groups={"lux": ["other"]},
            quota_groups=[
                {"status_slug": "lux", "provider_group": "claude", "weekly_quota_usd": 1}
            ],
        )
    )
    assert cfg is not None
    assert cfg.effective_card_provider_groups() == {"lux": ["other", "claude"]}


def test_card_provider_groups_drops_empty_lists():
    cfg = CchStatusConfig.from_dict(
        _enabled(
            card_provider_groups={"opus": [], "sonnet": ["claude"]},
            quota_groups=[{"status_slug": "opus", "weekly_quota_usd": 1}],
        )
    )
    assert cfg is not None
    assert cfg.card_provider_groups == {"sonnet": ["claude"]}


def test_card_provider_groups_all_empty_is_unset():
    cfg = CchStatusConfig.from_dict(
        _enabled(card_provider_groups={"opus": []}, quota_groups=[])
    )
    assert cfg is not None
    assert cfg.quota_groups == ()
    assert cfg.card_provider_groups is None


@pytest.mark.parametrize(
    "raw",
    [
        {"status_group_slug": "opus"},
        {"provider_group": "claude"},
        {"weekly_quota_usd": 200},
        {"auto_manage_provider_group_quota": False},
        {"auto_disable_threshold_pct": 100},
        {"status_group_provider_groups": {"opus": ["claude"]}},
    ],
)
def test_legacy_quota_keys_are_rejected(raw: dict):
    with pytest.raises(ValueError, match="quota_groups"):
        CchStatusConfig.from_dict(_enabled(**raw))


def test_duplicate_status_slug_and_provider_group_are_rejected():
    with pytest.raises(ValueError, match="duplicate status_slug"):
        CchStatusConfig.from_dict(
            _enabled(
                quota_groups=[
                    {"status_slug": "opus", "provider_group": "claude", "weekly_quota_usd": 1},
                    {"status_slug": "opus", "provider_group": "glm", "weekly_quota_usd": 1},
                ]
            )
        )
    with pytest.raises(ValueError, match="duplicate provider_group"):
        CchStatusConfig.from_dict(
            _enabled(
                quota_groups=[
                    {"status_slug": "opus", "provider_group": "claude", "weekly_quota_usd": 1},
                    {"status_slug": "sonnet", "provider_group": "claude", "weekly_quota_usd": 1},
                ]
            )
        )


@pytest.mark.parametrize(
    "entry, message",
    [
        ({"provider_group": "claude", "weekly_quota_usd": 1}, "status_slug"),
        ({"status_slug": "opus", "weekly_quota_usd": 0}, "must be > 0"),
        ({"status_slug": "opus", "weekly_quota_usd": 1, "disable_at_pct": 0}, "must be in"),
        ({"status_slug": "opus", "weekly_quota_usd": 1, "disable_at_pct": 101}, "must be in"),
    ],
)
def test_quota_group_bounds(entry: dict, message: str):
    with pytest.raises(ValueError, match=message):
        CchStatusConfig.from_dict(_enabled(quota_groups=[entry]))


def test_checked_in_config_links_lux_to_claude():
    raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg = CchStatusConfig.from_dict(raw["cch_status"])
    assert cfg is not None
    assert cfg.card_provider_groups is None
    assert cfg.quota_groups == (
        QuotaGroupConfig(
            status_slug="lux",
            provider_group="claude",
            weekly_quota_usd=Decimal("100"),
            auto_manage=True,
            disable_at_pct=Decimal("100"),
        ),
    )
    assert cfg.effective_card_provider_groups() == {"lux": ["claude"]}


def test_quota_usage_summary_ignores_other_groups_providers():
    usages = {
        1: {"costWeekly": {"current": "10.50"}},
        2: {"costWeekly": {"current": "40"}},
    }
    summary = quota_usage_summary([1], usages, Decimal("200"))
    assert summary == {
        "used": Decimal("10.50"),
        "remaining": Decimal("189.50"),
        "usage_pct": Decimal("10.50") / Decimal("200") * Decimal("100"),
    }


class _FakeCch:
    def __init__(self) -> None:
        self.batch_calls: list[list[int]] = []
        self.enabled_calls: list[tuple[int, bool]] = []

    async def list_providers(self) -> list[dict]:
        return [
            {"id": 1, "groupTag": "claude", "isEnabled": True},
            {"id": 2, "groupTag": "claude", "isEnabled": True},
            {"id": 3, "groupTag": "glm", "isEnabled": True},
        ]

    async def get_providers_limit_usage(self, provider_ids: list[int]) -> dict[int, dict]:
        self.batch_calls.append(list(provider_ids))
        return {
            1: {"costWeekly": {"current": "10"}},
            2: {"costWeekly": {"current": "5"}},
            3: {"costWeekly": {"current": "40"}},
        }

    async def set_provider_enabled(self, provider_id: int, enabled: bool) -> None:
        self.enabled_calls.append((provider_id, enabled))


def test_weekly_consumption_sums_each_group_from_one_batch():
    fake = _FakeCch()
    feature = CchStatusFeature(
        CchStatusConfig(
            base_url="http://cch.local",
            admin_token="token",
            quota_groups=(
                QuotaGroupConfig("opus", "claude", Decimal("200")),
                QuotaGroupConfig(
                    "glm",
                    "glm",
                    Decimal("50"),
                    auto_manage=True,
                    disable_at_pct=Decimal("50"),
                ),
            ),
        )
    )
    feature._cch = fake

    payload = asyncio.run(feature.get_weekly_consumption())

    assert fake.batch_calls == [[1, 2, 3]]
    assert [group["statusSlug"] for group in payload["groups"]] == ["opus", "glm"]
    assert payload["groups"][0]["providerIds"] == [1, 2]
    assert payload["groups"][0]["usedUsd"] == 15.0
    assert payload["groups"][0]["quotaUsd"] == 200.0
    assert payload["groups"][0]["remainingUsd"] == 185.0
    assert payload["groups"][0]["autoManage"]["enabled"] is False
    assert payload["groups"][1]["providerIds"] == [3]
    assert payload["groups"][1]["usedUsd"] == 40.0
    assert payload["groups"][1]["usagePct"] == 80.0
    assert payload["groups"][1]["autoManage"]["disabledProviderIds"] == [3]
    assert fake.enabled_calls == [(3, False)]
    assert feature._auto_disabled_provider_ids["claude"] == set()
    assert feature._auto_disabled_provider_ids["glm"] == {3}


def test_weekly_consumption_skips_cch_when_no_quota_groups():
    feature = CchStatusFeature(
        CchStatusConfig(base_url="http://cch.local", admin_token="token")
    )

    payload = asyncio.run(feature.get_weekly_consumption())

    assert payload["groups"] == []
    assert feature._cch is None
