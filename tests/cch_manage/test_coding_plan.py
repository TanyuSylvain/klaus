from cch_manage.coding_plan import (
    TIER_FIVE_HOUR,
    TIER_WEEKLY_LIMIT,
    detect_provider,
    kimi_usages_url,
    normalize_provider_name,
    parse_kimi_tiers,
    parse_minimax_tiers,
    parse_zhipu_token_tiers,
    zhipu_quota_from_body,
    zhipu_quota_host,
)


def test_kimi_usages_url():
    assert kimi_usages_url("https://api.kimi.com/coding") == "https://api.kimi.com/coding/v1/usages"
    assert kimi_usages_url("https://api.kimi.com/coding/") == "https://api.kimi.com/coding/v1/usages"
    # A bare host still resolves; the /coding prefix is implied.
    assert kimi_usages_url("https://api.kimi.com") == "https://api.kimi.com/coding/v1/usages"
    assert kimi_usages_url("") == "https://api.kimi.com/coding/v1/usages"


def test_detect_provider():
    assert detect_provider("https://api.kimi.com/coding/v1") == "kimi"
    assert detect_provider("https://open.bigmodel.cn/api/anthropic") == "glm"
    assert detect_provider("https://api.z.ai/api/anthropic") == "glm"
    assert detect_provider("https://api.minimaxi.com") == "minimax"
    assert detect_provider("https://api.minimax.io/v1") == "minimax"
    assert detect_provider("https://api.anthropic.com") is None


def test_normalize_provider_name():
    assert normalize_provider_name("zhipu") == "glm"
    assert normalize_provider_name("GLM") == "glm"
    assert normalize_provider_name("kimi") == "kimi"
    assert normalize_provider_name("unknown") is None


def test_parse_kimi_tiers():
    body = {
        "limits": [
            {
                "detail": {
                    "limit": 100,
                    "remaining": 40,
                    "resetTime": "2026-07-25T12:00:00Z",
                }
            }
        ],
        "usage": {
            "limit": 200,
            "remaining": 50,
            "resetTime": "2026-08-01T00:00:00Z",
        },
    }
    tiers = parse_kimi_tiers(body)
    assert len(tiers) == 2
    assert tiers[0].name == TIER_FIVE_HOUR
    assert tiers[0].utilization == 60.0
    assert tiers[1].name == TIER_WEEKLY_LIMIT
    assert tiers[1].utilization == 75.0


def test_parse_zhipu_token_tiers_by_unit():
    data = {
        "limits": [
            {
                "type": "TOKENS_LIMIT",
                "unit": 3,
                "number": 5,
                "percentage": 42.5,
                "nextResetTime": 1785000000000,
            },
            {
                "type": "TOKENS_LIMIT",
                "unit": 6,
                "number": 7,
                "percentage": 88.0,
                "nextResetTime": 1785600000000,
            },
        ]
    }
    tiers = parse_zhipu_token_tiers(data)
    assert [t.name for t in tiers] == [TIER_FIVE_HOUR, TIER_WEEKLY_LIMIT]
    assert tiers[0].utilization == 42.5
    assert tiers[1].utilization == 88.0
    assert tiers[0].resets_at is not None


def test_zhipu_quota_from_body_error():
    result = zhipu_quota_from_body({"success": False, "msg": "nope"})
    assert result.success is False
    assert "nope" in (result.error or "")


def test_zhipu_quota_host():
    assert zhipu_quota_host("https://open.bigmodel.cn/api/anthropic") == "https://open.bigmodel.cn"
    assert zhipu_quota_host("https://api.z.ai/api/anthropic") == "https://api.z.ai"


def test_parse_minimax_tiers():
    body = {
        "model_remains": [
            {
                "model_name": "video",
                "current_interval_remaining_percent": 10,
            },
            {
                "model_name": "general",
                "current_interval_remaining_percent": 25,
                "end_time": 1785000000000,
                "current_weekly_status": 1,
                "current_weekly_remaining_percent": 60,
                "weekly_end_time": 1785600000000,
            },
        ]
    }
    tiers = parse_minimax_tiers(body)
    assert len(tiers) == 2
    assert tiers[0].name == TIER_FIVE_HOUR
    assert tiers[0].utilization == 75.0
    assert tiers[1].name == TIER_WEEKLY_LIMIT
    assert tiers[1].utilization == 40.0


def test_parse_minimax_skips_weekly_when_status_not_active():
    body = {
        "model_remains": [
            {
                "model_name": "general",
                "current_interval_remaining_percent": 90,
                "current_weekly_status": 3,
                "current_weekly_remaining_percent": 100,
            }
        ]
    }
    tiers = parse_minimax_tiers(body)
    assert len(tiers) == 1
    assert tiers[0].name == TIER_FIVE_HOUR
