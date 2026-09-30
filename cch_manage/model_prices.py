import re
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
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
_RATIO_QUANTUM = Decimal("0.0001")
_OPENAI_LONG_CONTEXT_THRESHOLD = 272_000
_ANTHROPIC_LONG_CONTEXT_THRESHOLD = 200_000
_GPT_LONG_CONTEXT_FAMILIES = frozenset({"gpt", "gpt-pro"})
_LONG_CONTEXT_MARKERS = ("*", "†", "‡", "§", "¶", "‖")
# Base rate, 272k absolute rate, 200k absolute rate. Priority and flex keys are omitted.
_LONG_CONTEXT_KINDS = (
    (
        "input_cost_per_token",
        "input_cost_per_token_above_272k_tokens",
        "input_cost_per_token_above_200k_tokens",
    ),
    (
        "output_cost_per_token",
        "output_cost_per_token_above_272k_tokens",
        "output_cost_per_token_above_200k_tokens",
    ),
    (
        "cache_read_input_token_cost",
        "cache_read_input_token_cost_above_272k_tokens",
        "cache_read_input_token_cost_above_200k_tokens",
    ),
    (
        "cache_creation_input_token_cost",
        "cache_creation_input_token_cost_above_272k_tokens",
        "cache_creation_input_token_cost_above_200k_tokens",
    ),
)
# Base rate, explicit multiplier, explicit absolute rate inside long_context_pricing.
_EXPLICIT_LONG_CONTEXT_KINDS = (
    ("input_cost_per_token", "input_multiplier", "input_cost_per_token"),
    ("output_cost_per_token", "output_multiplier", "output_cost_per_token"),
    ("cache_read_input_token_cost", "cache_read_input_multiplier", "cache_read_input_token_cost"),
    (
        "cache_creation_input_token_cost",
        "cache_creation_input_multiplier",
        "cache_creation_input_token_cost",
    ),
)


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
    long_context_threshold: int | None = None
    long_context_coefficient: Decimal | None = None

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
        tier = _long_context_tier(price_data)
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
            long_context_threshold=None if tier is None else tier[0],
            long_context_coefficient=None if tier is None else tier[1],
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
        return {
            "generatedAt": generated_at,
            "syncedAt": None,
            "models": [],
            "longContextNotes": [],
        }

    by_name = {row.model_name: row for row in stored_rows}
    synced_at = max(row.updated_at for row in stored_rows)
    public_models = collect_public_models(public_groups)
    markers = _long_context_markers(
        [by_name.get(model_name) for model_name, _slugs in public_models]
    )
    models: list[dict[str, Any]] = []
    for model_name, slugs in public_models:
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
                "longContext": _long_context_model(row, markers),
            }
        )
    return {
        "generatedAt": generated_at,
        "syncedAt": synced_at.isoformat(),
        "models": models,
        "longContextNotes": _long_context_notes(markers),
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


def _long_context_tier(price_data: dict[str, Any]) -> tuple[int, Decimal] | None:
    """Highest ratio above 1 across input, output, cache read, and cache write.

    Once input context crosses the threshold, CCH bills the whole request at the
    higher unit rates. A ratio of 1 is the same list price. Priority, flex, and
    nested vendor copies are not the top-level rates CCH bills.
    """
    explicit = _tier_from_long_context_pricing(price_data)
    if explicit is not None:
        return explicit
    return _tier_from_above_fields(price_data)


def _tier_from_long_context_pricing(price_data: dict[str, Any]) -> tuple[int, Decimal] | None:
    pricing = price_data.get("long_context_pricing")
    if not isinstance(pricing, dict):
        return None
    threshold = _threshold_tokens(pricing.get("threshold_tokens"))
    if threshold is None:
        return None
    ratios: list[Decimal] = []
    for base_key, multiplier_key, cost_key in _EXPLICIT_LONG_CONTEXT_KINDS:
        base = _optional_decimal(price_data, base_key)
        if base is None or base <= 0:
            continue
        explicit_cost = _optional_decimal(pricing, cost_key)
        if explicit_cost is not None:
            ratio = explicit_cost / base
        else:
            multiplier = _optional_decimal(pricing, multiplier_key)
            if multiplier is None:
                continue
            ratio = multiplier
        normalized = _normalize_ratio(ratio)
        if normalized > 1:
            ratios.append(normalized)
    if not ratios:
        return None
    return threshold, max(ratios)


def _tier_from_above_fields(price_data: dict[str, Any]) -> tuple[int, Decimal] | None:
    ratios: list[Decimal] = []
    saw_272k = False
    saw_200k = False
    for base_key, above_272_key, above_200_key in _LONG_CONTEXT_KINDS:
        premium_272 = _optional_decimal(price_data, above_272_key)
        premium_200 = _optional_decimal(price_data, above_200_key)
        if premium_272 is not None:
            saw_272k = True
            premium = premium_272
        elif premium_200 is not None:
            saw_200k = True
            premium = premium_200
        else:
            continue
        base = _optional_decimal(price_data, base_key)
        if base is None or base <= 0:
            continue
        normalized = _normalize_ratio(premium / base)
        if normalized > 1:
            ratios.append(normalized)
    if not ratios:
        return None
    family = price_data.get("model_family")
    if saw_272k or (isinstance(family, str) and family in _GPT_LONG_CONTEXT_FAMILIES):
        return _OPENAI_LONG_CONTEXT_THRESHOLD, max(ratios)
    if saw_200k:
        return _ANTHROPIC_LONG_CONTEXT_THRESHOLD, max(ratios)
    return None


def _threshold_tokens(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number <= 0:
        return None
    return int(number.to_integral_value(rounding=ROUND_FLOOR))


def _normalize_ratio(ratio: Decimal) -> Decimal:
    rounded = ratio.quantize(_RATIO_QUANTUM, rounding=ROUND_HALF_UP)
    nearest = rounded.to_integral_value(rounding=ROUND_HALF_UP)
    if abs(rounded - nearest) <= _RATIO_QUANTUM:
        return nearest
    return rounded


def _long_context_markers(
    rows: list[StoredModelPrice | None],
) -> dict[tuple[int, Decimal], str]:
    classes = {
        key
        for key in (_long_context_key(row) for row in rows)
        if key is not None
    }
    ordered = sorted(classes)
    return {key: _long_context_marker(index) for index, key in enumerate(ordered)}


def _long_context_key(row: StoredModelPrice | None) -> tuple[int, Decimal] | None:
    if row is None or row.long_context_threshold is None or row.long_context_coefficient is None:
        return None
    return row.long_context_threshold, row.long_context_coefficient


def _long_context_marker(index: int) -> str:
    if index < len(_LONG_CONTEXT_MARKERS):
        return _LONG_CONTEXT_MARKERS[index]
    return f"*{index + 1}"


def _long_context_model(
    row: StoredModelPrice | None,
    markers: dict[tuple[int, Decimal], str],
) -> dict[str, Any] | None:
    key = _long_context_key(row)
    if key is None:
        return None
    return {
        "marker": markers[key],
        "threshold": key[0],
        "coefficient": decimal_string(key[1]),
    }


def _long_context_notes(markers: dict[tuple[int, Decimal], str]) -> list[dict[str, str]]:
    return [
        {"marker": marker, "text": _long_context_footnote(threshold, coefficient)}
        for (threshold, coefficient), marker in markers.items()
    ]


def _long_context_footnote(threshold: int, coefficient: Decimal) -> str:
    return (
        "当单次请求的输入上下文超过"
        f"{_format_context_threshold(threshold)}时，整次请求改按"
        f"{decimal_string(coefficient)}倍单价计费。"
    )


def _format_context_threshold(threshold: int) -> str:
    if threshold > 0 and threshold % 1_000_000 == 0:
        return f"{threshold // 1_000_000}M"
    if threshold > 0 and threshold % 1_000 == 0:
        return f"{decimal_string(Decimal(threshold) / Decimal(1000))}K"
    return str(threshold)


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
