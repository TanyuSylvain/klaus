import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from cch_manage.price_coefficients import coefficient_label
from cch_manage.utils import decimal_from_any, provider_enabled, provider_groups


PER_CALL_MODES = frozenset({"image_generation", "image_edit"})
PER_CALL_FIELDS = (
    "input_cost_per_request",
    "output_cost_per_image",
    "input_cost_per_image",
)
PRICE_RESPONSE_KEYS = (
    "inputPerM",
    "outputPerM",
    "cacheReadPerM",
    "cacheWritePerM",
    "perCall",
)
_MILLION = Decimal("1000000")
_DISPLAY_QUANTUM = Decimal("0.000000000001")


@dataclass(frozen=True)
class StoredModelPrice:
    model_name: str
    billing: str
    input_per_m: Decimal | None
    output_per_m: Decimal | None
    cache_read_per_m: Decimal | None
    cache_write_per_m: Decimal | None
    per_call: Decimal | None
    updated_at: datetime

    def amounts(self) -> dict[str, Decimal | None]:
        return {
            "inputPerM": self.input_per_m,
            "outputPerM": self.output_per_m,
            "cacheReadPerM": self.cache_read_per_m,
            "cacheWritePerM": self.cache_write_per_m,
            "perCall": self.per_call,
        }


def decimal_string(value: Decimal) -> str:
    quantized = value.quantize(_DISPLAY_QUANTUM)
    text = format(quantized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def collect_public_models(public_groups: list[dict[str, Any]]) -> list[tuple[str, list[str]]]:
    """Unique public model keys in status-card order, with every card slug."""
    order: list[str] = []
    groups_by_model: dict[str, list[str]] = {}
    for group in public_groups:
        slug = group.get("publicGroupSlug")
        if not isinstance(slug, str) or not slug:
            continue
        models = group.get("models")
        if not isinstance(models, list):
            continue
        for model in models:
            if not isinstance(model, dict):
                continue
            model_name = model.get("publicModelKey")
            if not isinstance(model_name, str) or not model_name:
                continue
            slugs = groups_by_model.get(model_name)
            if slugs is None:
                order.append(model_name)
                slugs = []
                groups_by_model[model_name] = slugs
            if slug not in slugs:
                slugs.append(slug)
    return [(model_name, groups_by_model[model_name]) for model_name in order]


def stored_price_from_data(
    model_name: str,
    price_data: dict[str, Any] | None,
    *,
    updated_at: datetime,
) -> StoredModelPrice:
    if not isinstance(price_data, dict):
        return _empty_row(model_name, "unknown", updated_at)

    mode = price_data.get("mode")
    input_token = _optional_decimal(price_data, "input_cost_per_token")
    output_token = _optional_decimal(price_data, "output_cost_per_token")
    per_call_values = [_optional_decimal(price_data, field) for field in PER_CALL_FIELDS]
    positive_token = _is_positive(input_token) or _is_positive(output_token)
    per_call_present = any(value is not None for value in per_call_values)
    if mode in PER_CALL_MODES or (not positive_token and per_call_present):
        return StoredModelPrice(
            model_name=model_name,
            billing="call",
            input_per_m=None,
            output_per_m=None,
            cache_read_per_m=None,
            cache_write_per_m=None,
            per_call=_per_call_unit(per_call_values),
            updated_at=updated_at,
        )
    if input_token is not None or output_token is not None:
        return StoredModelPrice(
            model_name=model_name,
            billing="token",
            input_per_m=_per_million(input_token),
            output_per_m=_per_million(output_token),
            cache_read_per_m=_per_million(_optional_decimal(price_data, "cache_read_input_token_cost")),
            cache_write_per_m=_per_million(
                _optional_decimal(price_data, "cache_creation_input_token_cost")
            ),
            per_call=None,
            updated_at=updated_at,
        )
    return _empty_row(model_name, "unknown", updated_at)


def provider_allows_model(provider: dict[str, Any], model_name: str) -> bool:
    rules = provider.get("allowedModels")
    if rules is None:
        rules = provider.get("allowed_models")
    if not isinstance(rules, list) or not rules:
        return False
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        pattern = rule.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            continue
        match_type = rule.get("matchType")
        if match_type is None:
            match_type = rule.get("match_type")
        if match_type == "exact" and pattern == model_name:
            return True
        if match_type == "regex":
            try:
                if re.search(pattern, model_name):
                    return True
            except re.error:
                continue
    return False


def coefficients_for_model(
    model_name: str,
    status_slugs: list[str],
    providers: list[dict[str, Any]],
    card_provider_groups: dict[str, list[str]] | None,
) -> list[Decimal]:
    tags: set[str] = set()
    for slug in status_slugs:
        mapped = (card_provider_groups or {}).get(slug)
        tags.update(mapped or [slug])
    found: set[Decimal] = set()
    for provider in providers:
        if not provider_enabled(provider):
            continue
        if not tags.intersection(provider_groups(provider)):
            continue
        if not provider_allows_model(provider, model_name):
            continue
        multiplier = decimal_from_any(provider.get("costMultiplier"))
        if multiplier <= 0:
            continue
        found.add(multiplier)
    return sorted(found)


def build_model_price_response(
    public_groups: list[dict[str, Any]],
    stored_rows: list[StoredModelPrice],
    providers: list[dict[str, Any]],
    card_provider_groups: dict[str, list[str]] | None,
    *,
    generated_at: str,
) -> dict[str, Any]:
    if not stored_rows:
        return {"generatedAt": generated_at, "syncedAt": None, "models": []}

    by_name = {row.model_name: row for row in stored_rows}
    synced_at = max(row.updated_at for row in stored_rows)
    models: list[dict[str, Any]] = []
    for model_name, slugs in collect_public_models(public_groups):
        row = by_name.get(model_name)
        coefficients = coefficients_for_model(
            model_name,
            slugs,
            providers,
            card_provider_groups,
        )
        models.append(
            {
                "model": model_name,
                "groups": slugs,
                "billing": row.billing if row is not None else "unknown",
                "prices": _price_strings(row),
                "adjusted": _adjusted_strings(row, coefficients),
                "coefficients": [coefficient_label(value) for value in coefficients],
            }
        )
    return {
        "generatedAt": generated_at,
        "syncedAt": synced_at.isoformat(),
        "models": models,
    }


async def fetch_price_data(client: Any, model_name: str) -> dict[str, Any] | None:
    page = 1
    page_size = 100
    while page <= 20:
        body = await client.list_model_prices(search=model_name, page=page, page_size=page_size)
        if not isinstance(body, dict):
            raise RuntimeError("CCH model prices returned unexpected data shape")
        items = body.get("items")
        if not isinstance(items, list):
            raise RuntimeError("CCH model prices returned unexpected data shape")
        for item in items:
            if not isinstance(item, dict) or item.get("modelName") != model_name:
                continue
            price_data = item.get("priceData")
            if price_data is None:
                return None
            if not isinstance(price_data, dict):
                raise RuntimeError("CCH model price payload is not an object")
            return price_data
        total = body.get("total")
        if not isinstance(total, int) or isinstance(total, bool) or page * page_size >= total or not items:
            return None
        page += 1
    return None


async def collect_model_price_rows(
    client: Any,
    public_groups: list[dict[str, Any]],
    *,
    updated_at: datetime,
) -> list[StoredModelPrice]:
    rows: list[StoredModelPrice] = []
    for model_name, _slugs in collect_public_models(public_groups):
        price_data = await fetch_price_data(client, model_name)
        rows.append(stored_price_from_data(model_name, price_data, updated_at=updated_at))
    return rows


async def sync_model_prices(
    client: Any,
    store: Any,
    public_groups: list[dict[str, Any]],
    *,
    updated_at: datetime,
) -> None:
    rows = await collect_model_price_rows(client, public_groups, updated_at=updated_at)
    await store.replace_all(rows)


def _empty_row(model_name: str, billing: str, updated_at: datetime) -> StoredModelPrice:
    return StoredModelPrice(
        model_name=model_name,
        billing=billing,
        input_per_m=None,
        output_per_m=None,
        cache_read_per_m=None,
        cache_write_per_m=None,
        per_call=None,
        updated_at=updated_at,
    )


def _optional_decimal(price_data: dict[str, Any], key: str) -> Decimal | None:
    if key not in price_data:
        return None
    value = price_data[key]
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _is_positive(value: Decimal | None) -> bool:
    return value is not None and value > 0


def _per_call_unit(values: list[Decimal | None]) -> Decimal | None:
    for value in values:
        if _is_positive(value):
            return value
    if any(value is not None and value == 0 for value in values):
        return Decimal("0")
    return None


def _per_million(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return value * _MILLION


def _price_strings(row: StoredModelPrice | None) -> dict[str, str | None]:
    amounts = row.amounts() if row is not None else {}
    return {
        key: decimal_string(amounts[key]) if isinstance(amounts.get(key), Decimal) else None
        for key in PRICE_RESPONSE_KEYS
    }


def _adjusted_strings(
    row: StoredModelPrice | None,
    coefficients: list[Decimal],
) -> dict[str, dict[str, str] | None]:
    amounts = row.amounts() if row is not None else {}
    adjusted: dict[str, dict[str, str] | None] = {}
    for key in PRICE_RESPONSE_KEYS:
        base = amounts.get(key)
        if not isinstance(base, Decimal) or not coefficients:
            adjusted[key] = None
            continue
        scaled = [base * coefficient for coefficient in coefficients]
        adjusted[key] = {
            "min": decimal_string(min(scaled)),
            "max": decimal_string(max(scaled)),
        }
    return adjusted
