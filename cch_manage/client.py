from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable

import httpx

from cch_manage.utils import provider_groups

PostAction = Callable[[str, dict[str, Any]], Awaitable[Any]]


async def post_action(
    client: httpx.AsyncClient,
    base_url: str,
    admin_token: str,
    path: str,
    payload: dict[str, Any],
) -> Any:
    response = await client.post(
        base_url + path,
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {admin_token}",
            "cookie": f"auth-token={admin_token}",
        },
        json=payload,
    )
    response.raise_for_status()

    body = response.json()
    if not isinstance(body, dict) or body.get("ok") is not True:
        raise RuntimeError(f"CCH action failed: {body.get('error', 'unknown error')}")
    return body.get("data")


async def fetch_public_status(
    client: httpx.AsyncClient,
    base_url: str,
    query: str,
) -> tuple[dict[str, Any], int]:
    url = f"{base_url}/api/public-status"
    if query:
        url = f"{url}?{query}"
    response = await client.get(url)
    if response.status_code not in (200, 400, 503):
        response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("CCH public status returned unexpected data shape")
    return data, response.status_code


async def get_public_status_groups(get_public_status_payload: Callable[[str], Awaitable[dict[str, Any]]]) -> list[dict[str, Any]]:
    data = await get_public_status_payload("include=groups")
    groups = data.get("groups") if isinstance(data, dict) else None
    return [group for group in groups if isinstance(group, dict)] if isinstance(groups, list) else []


async def get_claude_group_providers(
    post: PostAction,
    provider_group: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    data = await post("/api/actions/providers/getProviders", {})
    if not isinstance(data, list):
        raise RuntimeError("CCH getProviders returned unexpected data shape")

    providers = [provider for provider in data if isinstance(provider, dict)]
    return providers, [
        provider for provider in providers
        if provider_group in provider_groups(provider)
    ]


async def get_provider_weekly_usage(post: PostAction, provider_id: int) -> Decimal:
    data = await post(
        "/api/actions/providers/getProviderLimitUsage",
        {"providerId": provider_id},
    )
    if not isinstance(data, dict):
        raise RuntimeError("CCH getProviderLimitUsage returned unexpected data shape")

    weekly = data.get("costWeekly")
    if not isinstance(weekly, dict):
        raise RuntimeError("CCH getProviderLimitUsage response has no costWeekly object")

    current = weekly.get("current")
    try:
        used = Decimal(str(current if current is not None else "0"))
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError(f"CCH costWeekly.current is not numeric: {current!r}") from exc

    return used


async def set_provider_enabled(post: PostAction, provider_id: int, enabled: bool) -> None:
    await post(
        "/api/actions/providers/editProvider",
        {"providerId": provider_id, "is_enabled": enabled},
    )
