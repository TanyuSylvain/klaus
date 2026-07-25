import pytest

from cch_manage.config import CchStatusConfig, UsageQueryConfig


def test_parse_usage_monitors_auto_detect():
    cfg = CchStatusConfig.from_dict(
        {
            "enabled": True,
            "base_url": "http://cch.local",
            "admin_token": "token",
            "usage_monitors": [
                {
                    "provider_group": "claude",
                    "base_url": "https://open.bigmodel.cn/api/anthropic",
                    "api_key": "key-1",
                },
                {
                    "provider_group": "kimi",
                    "base_url": "https://api.kimi.com/coding",
                    "api_key": "key-2",
                    "provider": "kimi",
                },
            ],
            "usage_query": {
                "base_interval_seconds": 120,
                "min_interval_seconds": 20,
            },
        }
    )
    assert cfg is not None
    assert len(cfg.usage_monitors) == 2
    assert cfg.usage_monitors[0].provider == "glm"
    assert cfg.usage_monitors[1].provider == "kimi"
    assert cfg.usage_query.base_interval_seconds == 120
    assert cfg.usage_query.min_interval_seconds == 20


def test_parse_usage_monitors_provider_only_uses_default_base_url():
    cfg = CchStatusConfig.from_dict(
        {
            "enabled": True,
            "base_url": "http://cch.local",
            "admin_token": "token",
            "usage_monitors": [
                {"provider_group": "claude", "provider": "glm", "api_key": "k1"},
                {"provider_group": "kimi", "provider": "kimi", "api_key": "k2"},
                {"provider_group": "minimax", "provider": "minimax", "api_key": "k3"},
            ],
        }
    )
    assert cfg is not None
    assert cfg.usage_monitors[0].base_url == "https://open.bigmodel.cn"
    assert cfg.usage_monitors[1].base_url == "https://api.kimi.com/coding"
    assert cfg.usage_monitors[2].base_url == "https://api.minimaxi.com"


def test_parse_usage_monitors_unknown_base_url_requires_provider():
    with pytest.raises(ValueError, match="cannot detect provider"):
        CchStatusConfig.from_dict(
            {
                "enabled": True,
                "base_url": "http://cch.local",
                "admin_token": "token",
                "usage_monitors": [
                    {
                        "provider_group": "x",
                        "base_url": "https://example.com",
                        "api_key": "k",
                    }
                ],
            }
        )


def test_parse_usage_monitors_requires_provider_or_base_url():
    with pytest.raises(ValueError, match="set provider"):
        CchStatusConfig.from_dict(
            {
                "enabled": True,
                "base_url": "http://cch.local",
                "admin_token": "token",
                "usage_monitors": [
                    {"provider_group": "x", "api_key": "k"},
                ],
            }
        )


def test_parse_usage_monitors_rejects_duplicate_group():
    with pytest.raises(ValueError, match="duplicate provider_group"):
        CchStatusConfig.from_dict(
            {
                "enabled": True,
                "base_url": "http://cch.local",
                "admin_token": "token",
                "usage_monitors": [
                    {"provider_group": "claude", "provider": "glm", "api_key": "k1"},
                    {"provider_group": "claude", "provider": "kimi", "api_key": "k2"},
                ],
            }
        )


def test_usage_query_min_gt_base_invalid():
    with pytest.raises(ValueError, match="min_interval_seconds"):
        UsageQueryConfig.from_dict(
            {"base_interval_seconds": 30, "min_interval_seconds": 60}
        )


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ({"disable_threshold_pct": 0}, "disable_threshold_pct"),
        ({"disable_threshold_pct": 150}, "disable_threshold_pct"),
        ({"high_usage_threshold_pct": 0}, "high_usage_threshold_pct"),
        ({"high_usage_threshold_pct": 101}, "high_usage_threshold_pct"),
        (
            {"high_usage_threshold_pct": 95, "disable_threshold_pct": 90},
            "high_usage_threshold_pct",
        ),
        ({"request_timeout_seconds": 0}, "request_timeout_seconds"),
    ],
)
def test_usage_query_threshold_validation(raw, match):
    with pytest.raises(ValueError, match=match):
        UsageQueryConfig.from_dict(raw)
