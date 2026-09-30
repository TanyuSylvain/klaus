import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from cch_manage.config import CchStatusConfig
from cch_manage.feature import CchStatusFeature
from cch_manage.model_price_store import PostgresModelPriceStore
from cch_manage.model_prices import (
    StoredModelPrice,
    build_model_price_response,
    collect_model_price_rows,
    decimal_string,
    fetch_price_data,
    stored_price_from_data,
    sync_model_prices,
)
from cch_manage.status_page import inject_status_script


NOW = datetime(2026, 9, 29, 8, 0, tzinfo=UTC)


def _token_price() -> dict:
    return {
        "mode": "chat",
        "input_cost_per_token": "0.000008",
        "output_cost_per_token": "0.000028",
        "cache_read_input_token_cost": "0.000002",
        "search_context_cost_per_query": {"low": "0.01"},
    }


def test_token_price_converts_per_million_and_leaves_missing_cache_write_blank():
    row = stored_price_from_data("glm-5.3", _token_price(), updated_at=NOW)

    assert row.billing == "token"
    assert decimal_string(row.input_per_m) == "8"
    assert decimal_string(row.output_per_m) == "28"
    assert decimal_string(row.cache_read_per_m) == "2"
    assert row.cache_write_per_m is None
    assert row.per_call is None


def test_image_price_uses_per_request_cost_ahead_of_a_zero_per_image_cost():
    row = stored_price_from_data(
        "gpt-image-2",
        {
            "mode": "image_generation",
            "output_cost_per_image": 0,
            "input_cost_per_request": 1,
            "pricing": {"openai": {"input_cost_per_token": "0.000005"}},
        },
        updated_at=NOW,
    )

    assert row.billing == "call"
    assert row.per_call == Decimal("1")
    assert row.input_per_m is None
    assert row.output_per_m is None
    assert row.cache_read_per_m is None
    assert row.cache_write_per_m is None


def test_per_call_price_is_zero_when_every_per_call_field_is_zero():
    row = stored_price_from_data(
        "image-free",
        {
            "mode": "image_generation",
            "input_cost_per_request": 0,
            "output_cost_per_image": 0,
        },
        updated_at=NOW,
    )

    assert row.billing == "call"
    assert row.per_call == Decimal("0")


def test_missing_price_record_is_still_a_row():
    row = stored_price_from_data("k3", None, updated_at=NOW)

    assert row.billing == "unknown"
    assert row.input_per_m is None
    assert row.per_call is None


def test_response_applies_enabled_matching_coefficients_and_keeps_card_order():
    groups = [
        {
            "publicGroupSlug": "lux",
            "models": [{"publicModelKey": "gpt-6-sol"}],
        },
        {
            "publicGroupSlug": "prem",
            "models": [
                {"publicModelKey": "gpt-6-sol"},
                {"publicModelKey": "gpt-image-2"},
            ],
        },
    ]
    stored = [
        stored_price_from_data("gpt-6-sol", _token_price(), updated_at=NOW),
        stored_price_from_data(
            "gpt-image-2",
            {"mode": "image_generation", "input_cost_per_request": "2"},
            updated_at=NOW,
        ),
    ]
    providers = [
        {
            "isEnabled": True,
            "groupTag": "claude",
            "costMultiplier": "0.2",
            "allowedModels": [{"pattern": "gpt-6-sol", "matchType": "exact"}],
        },
        {
            "isEnabled": True,
            "groupTag": "claude",
            "costMultiplier": "0.5",
            "allowedModels": [{"pattern": "gpt-6-sol", "matchType": "exact"}],
        },
        {
            "isEnabled": False,
            "groupTag": "claude",
            "costMultiplier": "1.5",
            "allowedModels": [{"pattern": "gpt-6-sol", "matchType": "exact"}],
        },
        {
            "isEnabled": True,
            "groupTag": "glm",
            "costMultiplier": "9",
            "allowedModels": [{"pattern": "gpt-6-sol", "matchType": "exact"}],
        },
        {
            "isEnabled": True,
            "groupTag": "codex",
            "costMultiplier": "0.2",
            "allowedModels": [{"pattern": "^gpt-6-sol$", "matchType": "regex"}],
        },
        {
            "isEnabled": True,
            "groupTag": "image",
            "costMultiplier": "0.1",
            "allowedModels": [{"pattern": "[", "matchType": "regex"}],
        },
    ]

    payload = build_model_price_response(
        groups,
        stored,
        providers,
        {"lux": ["claude"], "prem": ["codex"]},
        generated_at="2026-09-29T16:00:00+08:00",
    )

    assert [model["model"] for model in payload["models"]] == ["gpt-6-sol", "gpt-image-2"]
    sol = payload["models"][0]
    assert sol["groups"] == ["lux", "prem"]
    assert sol["coefficients"] == ["0.2", "0.5"]
    assert sol["prices"]["inputPerM"] == "8"
    assert sol["prices"]["cacheWritePerM"] is None
    assert sol["prices"]["perCall"] is None
    assert sol["adjusted"]["inputPerM"] == {"min": "1.6", "max": "4"}
    assert sol["adjusted"]["perCall"] is None
    image = payload["models"][1]
    assert image["billing"] == "call"
    assert image["prices"]["perCall"] == "2"
    assert image["prices"]["inputPerM"] is None
    assert image["coefficients"] == []
    assert image["adjusted"]["perCall"] is None
    assert payload["syncedAt"] == NOW.isoformat()


def test_empty_store_reports_that_prices_have_not_synced():
    payload = build_model_price_response(
        [{"publicGroupSlug": "glm", "models": [{"publicModelKey": "glm-5.3"}]}],
        [],
        [],
        None,
        generated_at="2026-09-29T16:00:00+08:00",
    )

    assert payload["models"] == []
    assert payload["syncedAt"] is None


def test_fetch_price_data_follows_pages_until_the_exact_model():
    class Client:
        def __init__(self) -> None:
            self.pages: list[int] = []

        async def list_model_prices(self, *, search: str, page: int, page_size: int) -> dict:
            assert search == "glm-5.3"
            assert page_size == 100
            self.pages.append(page)
            if page == 1:
                return {
                    "items": [{"modelName": "glm-5.3-flash", "priceData": {"mode": "chat"}}],
                    "total": 150,
                    "pageSize": 100,
                }
            return {
                "items": [{"modelName": "glm-5.3", "priceData": {"mode": "chat", "input_cost_per_token": "0.000001"}}],
                "total": 150,
                "pageSize": 100,
            }

    client = Client()
    price_data = asyncio.run(fetch_price_data(client, "glm-5.3"))

    assert client.pages == [1, 2]
    assert price_data is not None
    assert price_data["input_cost_per_token"] == "0.000001"


def test_sync_replaces_stored_rows_only_after_a_complete_fetch():
    groups = [{"publicGroupSlug": "glm", "models": [{"publicModelKey": "glm-5.3"}]}]

    class Client:
        async def list_model_prices(self, *, search: str, page: int, page_size: int) -> dict:
            return {
                "items": [{"modelName": search, "priceData": _token_price()}],
                "total": 1,
                "pageSize": page_size,
            }

    class Store:
        def __init__(self) -> None:
            self.rows: list[str] = ["previous"]
            self.calls = 0

        async def replace_all(self, rows: list[StoredModelPrice]) -> None:
            self.calls += 1
            self.rows = [row.model_name for row in rows]

    store = Store()
    asyncio.run(sync_model_prices(Client(), store, groups, updated_at=NOW))

    assert store.calls == 1
    assert store.rows == ["glm-5.3"]


def test_sync_keeps_previous_rows_when_a_price_fetch_fails():
    groups = [{"publicGroupSlug": "glm", "models": [{"publicModelKey": "glm-5.3"}]}]

    class Client:
        async def list_model_prices(self, *, search: str, page: int, page_size: int) -> dict:
            raise RuntimeError("cch down")

    class Store:
        def __init__(self) -> None:
            self.rows = ["previous"]
            self.calls = 0

        async def replace_all(self, rows: list[StoredModelPrice]) -> None:
            self.calls += 1
            self.rows = rows

    store = Store()
    with pytest.raises(RuntimeError, match="cch down"):
        asyncio.run(sync_model_prices(Client(), store, groups, updated_at=NOW))

    assert store.calls == 0
    assert store.rows == ["previous"]


def test_collect_lists_a_public_model_with_no_price_record():
    groups = [{"publicGroupSlug": "kimi", "models": [{"publicModelKey": "k3"}]}]

    class Client:
        async def list_model_prices(self, *, search: str, page: int, page_size: int) -> dict:
            return {"items": [], "total": 0, "pageSize": page_size}

    rows = asyncio.run(collect_model_price_rows(Client(), groups, updated_at=NOW))

    assert len(rows) == 1
    assert rows[0].model_name == "k3"
    assert rows[0].billing == "unknown"


def test_model_price_config_parses_and_defaults_the_interval():
    cfg = CchStatusConfig.from_dict(
        {
            "enabled": True,
            "base_url": "http://cch.local",
            "admin_token": "token",
            "model_prices": {"database_url": "postgresql://llm_proxy@127.0.0.1:5432/llm_proxy"},
        }
    )

    assert cfg is not None
    assert cfg.model_prices is not None
    assert cfg.model_prices.database_url == "postgresql://llm_proxy@127.0.0.1:5432/llm_proxy"
    assert cfg.model_prices.update_interval_seconds == 21600


def test_model_price_config_is_optional_and_rejects_a_short_interval():
    cfg = CchStatusConfig.from_dict(
        {"enabled": True, "base_url": "http://cch.local", "admin_token": "token"}
    )

    assert cfg is not None
    assert cfg.model_prices is None
    with pytest.raises(ValueError, match="update_interval_seconds"):
        CchStatusConfig.from_dict(
            {
                "enabled": True,
                "base_url": "http://cch.local",
                "admin_token": "token",
                "model_prices": {
                    "database_url": "postgresql://llm_proxy@127.0.0.1/llm_proxy",
                    "update_interval_seconds": 30,
                },
            }
        )


def test_status_endpoint_reads_the_store_without_calling_model_prices():
    feature = CchStatusFeature(CchStatusConfig(base_url="http://cch.local", admin_token="token"))
    stored = stored_price_from_data("glm-5.3", _token_price(), updated_at=NOW)

    class Store:
        async def list_prices(self) -> list[StoredModelPrice]:
            return [stored]

    class Cch:
        def __init__(self) -> None:
            self.price_calls = 0

        async def list_providers(self) -> list[dict]:
            return [
                {
                    "isEnabled": True,
                    "groupTag": "glm",
                    "costMultiplier": "0.001",
                    "allowedModels": [{"pattern": "glm-5.3", "matchType": "exact"}],
                }
            ]

        async def list_model_prices(self, **kwargs: object) -> dict:
            self.price_calls += 1
            return {"items": [], "total": 0}

    cch = Cch()
    feature._model_price_store = Store()
    feature._cch = cch

    async def public_status(_query: str) -> dict:
        return {"groups": [{"publicGroupSlug": "glm", "models": [{"publicModelKey": "glm-5.3"}]}]}

    feature._get_public_status_payload = public_status  # type: ignore[method-assign]
    payload = asyncio.run(feature.get_model_prices())

    assert cch.price_calls == 0
    assert payload["models"][0]["prices"]["inputPerM"] == "8"
    assert payload["models"][0]["adjusted"]["inputPerM"] == {"min": "0.008", "max": "0.008"}
    assert payload["models"][0]["coefficients"] == ["0.001"]


def test_status_script_includes_the_price_table_control():
    html = inject_status_script("<body></body>", ["lux"], 300)

    assert "模型价格表" in html
    assert "/cch/status/model-prices" in html
    assert "minmax(0, 6fr) minmax(0, 4fr)" in html
    assert "var(--cch-status-width) * 4 / 6" in html
    assert "font-size:0.875rem" in html
    assert 'panel.style.gridRow = "3"' in html
    assert "cch-weekly-leaderboard" in html
    assert "background:#2563eb" in html
    assert "background:#e5e7eb" in html
    assert html.rfind("paintPriceSwitch()") > html.find("shell.appendChild(panel)")


def _astra_price() -> dict:
    return {
        "mode": "chat",
        "model_family": "gpt-sol",
        "input_cost_per_token": "0.00001",
        "output_cost_per_token": "0.00005",
        "cache_creation_input_token_cost": "0.0000125",
        "cache_read_input_token_cost": "0.000001",
        "input_cost_per_token_above_272k_tokens": "0.00001",
        "output_cost_per_token_above_272k_tokens": "0.000075",
        "cache_creation_input_token_cost_above_272k_tokens": "0.000025",
        "cache_read_input_token_cost_above_272k_tokens": "0.000002",
    }


def test_long_context_tier_keeps_the_highest_ratio_above_one():
    row = stored_price_from_data("gpt-6-astra", _astra_price(), updated_at=NOW)

    assert row.billing == "token"
    assert row.long_context_threshold == 272_000
    assert row.long_context_coefficient == Decimal("2")
    assert decimal_string(row.input_per_m) == "10"
    assert decimal_string(row.output_per_m) == "50"


def test_long_context_tier_skips_a_cache_kind_without_its_own_above_rate():
    row = stored_price_from_data(
        "gpt-6-luna",
        {
            "mode": "chat",
            "model_family": "gpt-luna",
            "input_cost_per_token": "0.0000001",
            "output_cost_per_token": "0.0000005",
            "cache_creation_input_token_cost": "0.000000125",
            "cache_read_input_token_cost": "0.00000001",
            "input_cost_per_token_above_272k_tokens": "0.000002",
            "output_cost_per_token_above_272k_tokens": "0.000009",
            "cache_read_input_token_cost_above_272k_tokens": "0.0000002",
            "input_cost_per_token_above_272k_tokens_priority": "0.000004",
            "output_cost_per_token_above_272k_tokens_priority": "0.000018",
        },
        updated_at=NOW,
    )

    assert row.long_context_threshold == 272_000
    assert row.long_context_coefficient == Decimal("20")


def test_long_context_ratio_snaps_float_dust_to_an_integer():
    row = stored_price_from_data(
        "gpt-6-sol",
        {
            "mode": "chat",
            "input_cost_per_token": "0.000002",
            "output_cost_per_token": "0.00001",
            "cache_read_input_token_cost": "0.0000002",
            "cache_read_input_token_cost_above_272k_tokens": "0.00000199998",
        },
        updated_at=NOW,
    )

    assert row.long_context_threshold == 272_000
    assert row.long_context_coefficient == Decimal("10")


def test_long_context_threshold_is_200k_when_only_200k_fields_exist():
    row = stored_price_from_data(
        "claude-sonnet",
        {
            "mode": "chat",
            "model_family": "claude",
            "input_cost_per_token": "0.000003",
            "output_cost_per_token": "0.000015",
            "input_cost_per_token_above_200k_tokens": "0.000006",
            "output_cost_per_token_above_200k_tokens": "0.0000225",
        },
        updated_at=NOW,
    )

    assert row.long_context_threshold == 200_000
    assert row.long_context_coefficient == Decimal("2")


def test_gpt_family_with_only_200k_fields_uses_the_272k_threshold():
    row = stored_price_from_data(
        "gpt-5",
        {
            "mode": "chat",
            "model_family": "gpt",
            "input_cost_per_token": "0.000002",
            "output_cost_per_token": "0.000008",
            "input_cost_per_token_above_200k_tokens": "0.000004",
        },
        updated_at=NOW,
    )

    assert row.long_context_threshold == 272_000
    assert row.long_context_coefficient == Decimal("2")


def test_flat_long_context_ratio_is_not_a_tier():
    row = stored_price_from_data(
        "flat",
        {
            "mode": "chat",
            "input_cost_per_token": "0.00001",
            "output_cost_per_token": "0.00005",
            "input_cost_per_token_above_272k_tokens": "0.00001",
            "output_cost_per_token_above_272k_tokens": "0.00005",
            "cache_creation_input_token_cost_above_272k_tokens": "0.00002",
        },
        updated_at=NOW,
    )

    assert row.long_context_threshold is None
    assert row.long_context_coefficient is None


def test_priority_flex_nested_and_one_hour_cache_are_not_a_tier():
    row = stored_price_from_data(
        "gpt-6-luna",
        {
            "mode": "chat",
            "model_family": "gpt",
            "input_cost_per_token": "0.0000001",
            "output_cost_per_token": "0.0000005",
            "input_cost_per_token_above_272k_tokens_priority": "0.000002",
            "output_cost_per_token_above_272k_tokens_priority": "0.000009",
            "input_cost_per_token_above_272k_tokens_flex": "0.00000005",
            "cache_creation_input_token_cost_above_1hr_above_272k_tokens": "0.00001",
            "pricing": {
                "openai": {"input_cost_per_token_above_272k_tokens": "0.000002"},
            },
        },
        updated_at=NOW,
    )

    assert row.long_context_threshold is None
    assert row.long_context_coefficient is None


def test_explicit_long_context_pricing_wins_when_its_ratio_exceeds_one():
    row = stored_price_from_data(
        "explicit",
        {
            "mode": "chat",
            "input_cost_per_token": "0.000002",
            "output_cost_per_token": "0.00001",
            "input_cost_per_token_above_272k_tokens": "0.00002",
            "long_context_pricing": {
                "threshold_tokens": 128_000,
                "input_multiplier": 3,
                "output_cost_per_token": "0.000015",
            },
        },
        updated_at=NOW,
    )

    assert row.long_context_threshold == 128_000
    assert row.long_context_coefficient == Decimal("3")


def test_explicit_long_context_pricing_at_one_falls_through_to_above_fields():
    row = stored_price_from_data(
        "fallback",
        {
            "mode": "chat",
            "model_family": "claude",
            "input_cost_per_token": "0.000002",
            "output_cost_per_token": "0.00001",
            "input_cost_per_token_above_200k_tokens": "0.000004",
            "long_context_pricing": {
                "threshold_tokens": 100_000,
                "input_multiplier": 1,
                "output_multiplier": "1",
            },
        },
        updated_at=NOW,
    )

    assert row.long_context_threshold == 200_000
    assert row.long_context_coefficient == Decimal("2")


def test_call_priced_model_and_a_missing_record_have_no_long_context_tier():
    image = stored_price_from_data(
        "gpt-image-2",
        {
            "mode": "image_generation",
            "input_cost_per_request": 1,
            "input_cost_per_token": "0.000001",
            "output_cost_per_token": "0.000002",
            "input_cost_per_token_above_272k_tokens": "0.00001",
        },
        updated_at=NOW,
    )
    missing = stored_price_from_data("k3", None, updated_at=NOW)

    assert image.long_context_threshold is None
    assert image.long_context_coefficient is None
    assert missing.long_context_threshold is None
    assert missing.long_context_coefficient is None


def test_response_groups_long_context_classes_and_writes_the_footnote():
    def price(above_key: str, above: str, *, family: str) -> dict:
        return {
            "mode": "chat",
            "model_family": family,
            "input_cost_per_token": "0.000001",
            "output_cost_per_token": "0.000002",
            above_key: above,
        }

    groups = [
        {
            "publicGroupSlug": "prem",
            "models": [
                {"publicModelKey": "wide-200"},
                {"publicModelKey": "wide-272-low"},
                {"publicModelKey": "wide-272-low-again"},
                {"publicModelKey": "wide-272-high"},
                {"publicModelKey": "plain"},
            ],
        }
    ]
    stored = [
        stored_price_from_data(
            "wide-200",
            price("input_cost_per_token_above_200k_tokens", "0.000002", family="claude"),
            updated_at=NOW,
        ),
        stored_price_from_data(
            "wide-272-low",
            price("input_cost_per_token_above_272k_tokens", "0.000002", family="gpt-sol"),
            updated_at=NOW,
        ),
        stored_price_from_data(
            "wide-272-low-again",
            price("input_cost_per_token_above_272k_tokens", "0.000002", family="gpt-sol"),
            updated_at=NOW,
        ),
        stored_price_from_data(
            "wide-272-high",
            price("input_cost_per_token_above_272k_tokens", "0.00001", family="gpt-sol"),
            updated_at=NOW,
        ),
        stored_price_from_data("plain", _token_price(), updated_at=NOW),
    ]

    payload = build_model_price_response(
        groups,
        stored,
        [],
        None,
        generated_at="2026-09-29T16:00:00+08:00",
    )

    by_name = {model["model"]: model for model in payload["models"]}
    assert by_name["wide-200"]["longContext"] == {
        "marker": "*",
        "threshold": 200_000,
        "coefficient": "2",
    }
    assert by_name["wide-272-low"]["longContext"]["marker"] == "†"
    assert by_name["wide-272-low-again"]["longContext"]["marker"] == "†"
    assert by_name["wide-272-high"]["longContext"] == {
        "marker": "‡",
        "threshold": 272_000,
        "coefficient": "10",
    }
    assert by_name["plain"]["longContext"] is None
    assert by_name["wide-272-low"]["prices"]["inputPerM"] == "1"
    assert payload["longContextNotes"] == [
        {
            "marker": "*",
            "text": "当单次请求的输入上下文超过200K时，整次请求改按2倍单价计费。",
        },
        {
            "marker": "†",
            "text": "当单次请求的输入上下文超过272K时，整次请求改按2倍单价计费。",
        },
        {
            "marker": "‡",
            "text": "当单次请求的输入上下文超过272K时，整次请求改按10倍单价计费。",
        },
    ]


def test_seventh_long_context_class_uses_a_numbered_marker():
    groups = [
        {
            "publicGroupSlug": "prem",
            "models": [{"publicModelKey": f"m{index}"} for index in range(7)],
        }
    ]
    stored = [
        stored_price_from_data(
            f"m{index}",
            {
                "mode": "chat",
                "model_family": "claude",
                "input_cost_per_token": "0.000001",
                "output_cost_per_token": "0.000002",
                "input_cost_per_token_above_200k_tokens": str(Decimal("0.000001") * (index + 2)),
            },
            updated_at=NOW,
        )
        for index in range(7)
    ]

    payload = build_model_price_response(groups, stored, [], None, generated_at="2026-09-29T16:00:00+08:00")

    assert [model["longContext"]["marker"] for model in payload["models"]] == [
        "*",
        "†",
        "‡",
        "§",
        "¶",
        "‖",
        "*7",
    ]
    assert payload["longContextNotes"][-1]["text"] == (
        "当单次请求的输入上下文超过200K时，整次请求改按8倍单价计费。"
    )


def test_price_store_maps_long_context_columns():
    row = stored_price_from_data("gpt-6-astra", _astra_price(), updated_at=NOW)

    args = PostgresModelPriceStore._row_args(row)
    assert args[-2:] == (272_000, Decimal("2"))

    record = {
        "model_name": "gpt-6-astra",
        "billing": "token",
        "input_per_m": Decimal("10"),
        "output_per_m": Decimal("50"),
        "cache_read_per_m": Decimal("1"),
        "cache_write_per_m": Decimal("12.5"),
        "per_call": None,
        "updated_at": NOW,
        "long_context_threshold": 272_000,
        "long_context_coefficient": Decimal("2"),
    }
    loaded = PostgresModelPriceStore._row_from_record(record)

    assert loaded.long_context_threshold == 272_000
    assert loaded.long_context_coefficient == Decimal("2")
    assert loaded.input_per_m == Decimal("10")


def test_status_script_renders_long_context_markers_and_footnotes():
    html = inject_status_script("<body></body>", ["lux"], 300)

    assert "model.longContext" in html
    assert "pricePayload.longContextNotes" in html
    assert "<sup" in html
