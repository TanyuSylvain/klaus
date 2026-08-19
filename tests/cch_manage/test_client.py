import asyncio
import json
from decimal import Decimal
from typing import Any, Callable

import httpx
import pytest

from cch_manage.client import CchApiError, CchClient, weekly_usage_decimal

Handler = Callable[[httpx.Request], httpx.Response]

TOKEN = "test-token"


def _make_client(handler: Handler, base_url: str = "http://cch.test") -> tuple[CchClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    return CchClient(http, base_url + "/", TOKEN), requests


def _json_response(payload: Any, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def _problem(status_code: int, error_code: str = "thing.not_found") -> httpx.Response:
    return httpx.Response(
        status_code,
        json={
            "type": "urn:claude-code-hub:problem:thing.not_found",
            "title": "Not found",
            "status": status_code,
            "detail": "Thing was not found.",
            "instance": "/api/v1/thing",
            "errorCode": error_code,
        },
    )


def _json_body(request: httpx.Request) -> Any:
    return json.loads(request.content.decode("utf-8"))


def test_weekly_usage_decimal_validates_cost_weekly():
    usage = {"costWeekly": {"current": 1.25, "limit": None}}
    assert weekly_usage_decimal(usage) == Decimal("1.25")

    assert weekly_usage_decimal({"costWeekly": {"current": None}}) == Decimal("0")

    with pytest.raises(RuntimeError, match="costWeekly"):
        weekly_usage_decimal({"costDaily": {"current": 1}})
    with pytest.raises(RuntimeError, match="not numeric"):
        weekly_usage_decimal({"costWeekly": {"current": "abc"}})


def test_list_providers_unwraps_items_and_sends_auth():
    provider = {"id": 1, "name": "p", "isEnabled": True, "groupTag": "claude"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/providers"
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.headers["cookie"] == f"auth-token={TOKEN}"
        return _json_response({"items": [provider, "junk"]})

    cch, _ = _make_client(handler)

    async def run() -> None:
        assert await cch.list_providers() == [provider]

    asyncio.run(run())


def test_list_providers_rejects_unexpected_shape():
    cch, _ = _make_client(lambda request: _json_response([{"id": 1}]))

    async def run() -> None:
        with pytest.raises(RuntimeError, match="unexpected data shape"):
            await cch.list_providers()

    asyncio.run(run())


def test_problem_response_raises_cch_api_error():
    cch, _ = _make_client(lambda request: _problem(404))

    error: CchApiError | None = None

    async def run() -> None:
        nonlocal error
        with pytest.raises(CchApiError) as excinfo:
            await cch.list_providers()
        error = excinfo.value

    asyncio.run(run())
    assert error is not None
    assert isinstance(error, RuntimeError)
    assert error.status_code == 404
    assert error.error_code == "thing.not_found"
    assert error.detail == "Thing was not found."
    assert "thing.not_found" in str(error)


def test_get_providers_limit_usage_posts_batch_body():
    usage = {"costWeekly": {"current": 2.5}}
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["json"] = _json_body(request)
        return _json_response(
            {"items": [{"id": 7, "usage": usage}, {"id": 8, "usage": usage}]}
        )

    cch, _ = _make_client(handler)

    async def run() -> None:
        assert await cch.get_providers_limit_usage([7, 8]) == {7: usage, 8: usage}

    asyncio.run(run())
    assert seen["method"] == "POST"
    assert seen["path"] == "/api/v1/providers/limit-usage:batch"
    assert seen["json"] == {"providerIds": [7, 8]}


def test_get_providers_limit_usage_skips_call_for_empty_ids():
    cch, requests = _make_client(lambda request: _json_response({"items": []}))

    async def run() -> None:
        assert await cch.get_providers_limit_usage([]) == {}

    asyncio.run(run())
    assert requests == []


def test_get_providers_limit_usage_raises_on_missing_provider():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"items": [{"id": 7, "usage": {}}]})

    cch, _ = _make_client(handler)

    async def run() -> None:
        with pytest.raises(RuntimeError, match="omitted requested providers: \\[8\\]"):
            await cch.get_providers_limit_usage([7, 8])

    asyncio.run(run())


def test_set_provider_enabled_fetches_csrf_then_patches_and_caches_token():
    seen: list[tuple[str, str, Any, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.method,
                request.url.path,
                _json_body(request) if request.content else None,
                request.headers.get("x-cch-csrf"),
            )
        )
        if request.url.path == "/api/v1/auth/csrf":
            return _json_response({"csrfToken": "csrf-1"})
        return _json_response({"id": 7, "isEnabled": False})

    cch, _ = _make_client(handler)

    async def run() -> None:
        await cch.set_provider_enabled(7, False)

    asyncio.run(run())
    assert seen == [
        ("GET", "/api/v1/auth/csrf", None, None),
        ("PATCH", "/api/v1/providers/7", {"is_enabled": False}, "csrf-1"),
    ]

    # The token is cached: a second mutation does not re-fetch it.
    seen.clear()

    async def run_again() -> None:
        await cch.set_provider_enabled(7, True)

    asyncio.run(run_again())
    assert seen == [("PATCH", "/api/v1/providers/7", {"is_enabled": True}, "csrf-1")]


def test_set_provider_enabled_refreshes_csrf_on_403_and_retries():
    csrf_fetches = 0
    patches: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal csrf_fetches
        if request.url.path == "/api/v1/auth/csrf":
            csrf_fetches += 1
            return _json_response({"csrfToken": f"csrf-{csrf_fetches}"})
        patches.append(request.headers.get("x-cch-csrf"))
        if len(patches) == 1:
            return _problem(403, "auth.csrf_invalid")
        return _json_response({"id": 7, "isEnabled": True})

    cch, _ = _make_client(handler)

    async def run() -> None:
        await cch.set_provider_enabled(7, True)

    asyncio.run(run())
    assert csrf_fetches == 2
    assert patches == ["csrf-1", "csrf-2"]


def test_set_provider_enabled_raises_after_second_403():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/csrf":
            return _json_response({"csrfToken": "csrf"})
        return _problem(403, "auth.csrf_invalid")

    cch, requests = _make_client(handler)
    error: CchApiError | None = None

    async def run() -> None:
        nonlocal error
        with pytest.raises(CchApiError) as excinfo:
            await cch.set_provider_enabled(7, False)
        error = excinfo.value

    asyncio.run(run())
    assert error is not None
    assert error.status_code == 403
    patches = [request for request in requests if request.method == "PATCH"]
    assert len(patches) == 2


def test_get_dashboard_statistics_sends_time_range():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return _json_response({"chartData": [], "users": []})

    cch, _ = _make_client(handler)

    async def run() -> None:
        assert await cch.get_dashboard_statistics("7days") == {"chartData": [], "users": []}

    asyncio.run(run())
    assert seen["path"] == "/api/v1/dashboard/statistics"
    assert seen["params"] == {"timeRange": "7days"}


def test_list_user_keys_unwraps_items():
    key = {"id": 11, "maskedKey": "sk-x", "isEnabled": True, "deletedAt": None}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/users/3/keys"
        return _json_response({"items": [key, "junk"]})

    cch, _ = _make_client(handler)

    async def run() -> None:
        assert await cch.list_user_keys(3) == [key]

    asyncio.run(run())


def test_reveal_key_returns_plaintext():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/keys/11:reveal"
        return _json_response({"key": "sk-plaintext"})

    cch, _ = _make_client(handler)

    async def run() -> None:
        assert await cch.reveal_key(11) == "sk-plaintext"

    asyncio.run(run())


def test_reveal_key_rejects_bad_shape():
    cch, _ = _make_client(lambda request: _json_response({}))

    async def run() -> None:
        with pytest.raises(RuntimeError, match="unexpected data shape"):
            await cch.reveal_key(11)

    asyncio.run(run())


def test_get_public_status_sends_no_auth_and_passes_status_through():
    seen: dict[str, Any] = {}
    result: tuple[dict[str, Any], int] | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = str(request.url.params)
        seen["auth"] = request.headers.get("authorization")
        seen["cookie"] = request.headers.get("cookie")
        return _json_response({"status": "rebuilding"}, status_code=503)

    cch, _ = _make_client(handler)

    async def run() -> None:
        nonlocal result
        result = await cch.get_public_status("include=groups")

    asyncio.run(run())
    assert seen["path"] == "/api/v1/public/status"
    assert seen["query"] == "include=groups"
    assert seen["auth"] is None
    assert seen["cookie"] is None
    assert result == ({"status": "rebuilding"}, 503)


def test_get_public_status_raises_on_unexpected_status():
    cch, _ = _make_client(lambda request: _problem(500, "internal.error"))

    async def run() -> None:
        with pytest.raises(CchApiError):
            await cch.get_public_status("include=groups")

    asyncio.run(run())
