import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from cch_manage.config import CchStatusConfig
from cch_manage.feature import CchStatusFeature
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
