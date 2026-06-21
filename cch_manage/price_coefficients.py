from decimal import Decimal
from typing import Any

from cch_manage.utils import decimal_from_any, provider_enabled, provider_groups


def extract_group_price_coefficients(
    providers: list[dict[str, Any]],
    public_groups: list[dict[str, Any]] | None = None,
    status_group_provider_groups: dict[str, list[str]] | None = None,
) -> dict[str, list[str]]:
    if public_groups:
        return extract_public_group_price_coefficients(
            providers,
            public_groups,
            status_group_provider_groups or {},
        )

    coefficients_by_group: dict[str, set[Decimal]] = {}

    for provider in providers:
        if not provider_enabled(provider):
            continue

        multiplier = decimal_from_any(provider.get("costMultiplier"))
        if multiplier <= 0:
            continue

        for group in provider_groups(provider):
            coefficients_by_group.setdefault(group, set()).add(multiplier)

    return {
        group: [coefficient_label(value) for value in sorted(coefficients)]
        for group, coefficients in sorted(coefficients_by_group.items())
        if coefficients
    }


def extract_public_group_price_coefficients(
    providers: list[dict[str, Any]],
    public_groups: list[dict[str, Any]],
    status_group_provider_groups: dict[str, list[str]],
) -> dict[str, list[str]]:
    enabled_providers = [provider for provider in providers if provider_enabled(provider)]
    coefficients_by_slug: dict[str, set[Decimal]] = {}

    for public_group in public_groups:
        slug = public_group.get("publicGroupSlug")
        if not isinstance(slug, str) or not slug:
            continue
        backing_provider_groups = set(status_group_provider_groups.get(slug) or [slug])
        group_coefficients: set[Decimal] = set()

        for provider in enabled_providers:
            if not backing_provider_groups.intersection(provider_groups(provider)):
                continue
            multiplier = decimal_from_any(provider.get("costMultiplier"))
            if multiplier > 0:
                group_coefficients.add(multiplier)

        if group_coefficients:
            coefficients_by_slug[slug] = group_coefficients

    return {
        slug: [coefficient_label(value) for value in sorted(coefficients)]
        for slug, coefficients in sorted(coefficients_by_slug.items())
    }


def coefficient_label(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return str(normalized.quantize(Decimal("1")))
    return format(normalized, "f")
