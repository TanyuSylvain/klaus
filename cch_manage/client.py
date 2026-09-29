from decimal import Decimal, InvalidOperation
from typing import Any

import httpx


class CchApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.detail = detail


def weekly_usage_decimal(usage: dict[str, Any]) -> Decimal:
    weekly = usage.get("costWeekly")
    if not isinstance(weekly, dict):
        raise RuntimeError("CCH provider limit usage response has no costWeekly object")

    current = weekly.get("current")
    if current is None:
        return Decimal("0")
    try:
        return Decimal(str(current))
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError(f"CCH costWeekly.current is not numeric: {current!r}") from exc


class CchClient:
    """Client for the CCH Management API (`/api/v1/...`).

    Success responses are bare JSON payloads; errors are RFC 7807
    `application/problem+json` and are raised as `CchApiError`.
    """

    def __init__(self, client: httpx.AsyncClient, base_url: str, admin_token: str) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._admin_token = admin_token
        self._csrf_token: str | None = None

    async def get_public_status(self, query: str) -> tuple[dict[str, Any], int]:
        path = "/api/v1/public/status"
        if query:
            path = f"{path}?{query}"
        response = await self._send("GET", path, auth=False)
        if response.status_code not in (200, 400, 503):
            self._raise_for_problem(response)

        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("CCH public status returned unexpected data shape")
        return data, response.status_code

    async def list_model_prices(
        self,
        *,
        search: str,
        page: int = 1,
        page_size: int = 100,
    ) -> dict[str, Any]:
        response = await self._send(
            "GET",
            "/api/v1/model-prices",
            params={"search": search, "page": page, "pageSize": page_size},
        )
        self._raise_for_problem(response)

        body = response.json()
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list):
            raise RuntimeError("CCH model prices returned unexpected data shape")
        return body

    async def list_providers(self) -> list[dict[str, Any]]:
        response = await self._send("GET", "/api/v1/providers")
        self._raise_for_problem(response)

        body = response.json()
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list):
            raise RuntimeError("CCH list providers returned unexpected data shape")
        return [provider for provider in items if isinstance(provider, dict)]

    async def get_providers_limit_usage(self, provider_ids: list[int]) -> dict[int, dict[str, Any]]:
        if not provider_ids:
            return {}

        response = await self._send(
            "POST",
            "/api/v1/providers/limit-usage:batch",
            json={"providerIds": provider_ids},
        )
        self._raise_for_problem(response)

        body = response.json()
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list):
            raise RuntimeError("CCH batch limit usage returned unexpected data shape")

        usages: dict[int, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            usage = item.get("usage")
            if isinstance(item_id, int) and not isinstance(item_id, bool) and isinstance(usage, dict):
                usages[item_id] = usage

        missing = [provider_id for provider_id in provider_ids if provider_id not in usages]
        if missing:
            raise RuntimeError(f"CCH batch limit usage omitted requested providers: {missing}")
        return usages

    async def set_provider_enabled(self, provider_id: int, enabled: bool) -> None:
        await self._mutate(
            "PATCH",
            f"/api/v1/providers/{provider_id}",
            {"is_enabled": enabled},
        )

    async def get_dashboard_statistics(self, time_range: str = "7days") -> dict[str, Any]:
        response = await self._send(
            "GET",
            "/api/v1/dashboard/statistics",
            params={"timeRange": time_range},
        )
        self._raise_for_problem(response)

        body = response.json()
        if not isinstance(body, dict):
            raise RuntimeError("CCH dashboard statistics returned unexpected data shape")
        return body

    async def get_usage_log_stats(
        self,
        *,
        provider_id: int,
        start_time_ms: int,
        end_time_ms: int,
    ) -> dict[str, Any]:
        response = await self._send(
            "GET",
            "/api/v1/usage-logs/stats",
            params={
                "providerId": provider_id,
                "startTime": start_time_ms,
                "endTime": end_time_ms,
            },
        )
        self._raise_for_problem(response)

        body = response.json()
        if not isinstance(body, dict):
            raise RuntimeError("CCH usage log stats returned unexpected data shape")
        return body

    async def list_user_keys(self, user_id: int) -> list[dict[str, Any]]:
        response = await self._send("GET", f"/api/v1/users/{user_id}/keys")
        self._raise_for_problem(response)

        body = response.json()
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list):
            raise RuntimeError("CCH user keys returned unexpected data shape")
        return [key for key in items if isinstance(key, dict)]

    async def reveal_key(self, key_id: int) -> str:
        response = await self._send("GET", f"/api/v1/keys/{key_id}:reveal")
        self._raise_for_problem(response)

        body = response.json()
        key = body.get("key") if isinstance(body, dict) else None
        if not isinstance(key, str) or not key:
            raise RuntimeError("CCH reveal key returned unexpected data shape")
        return key

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        csrf_token: str | None = None,
        auth: bool = True,
    ) -> httpx.Response:
        headers: dict[str, str] = {}
        if auth:
            headers.update(
                {
                    "authorization": f"Bearer {self._admin_token}",
                    "cookie": f"auth-token={self._admin_token}",
                }
            )
        if csrf_token is not None:
            headers["x-cch-csrf"] = csrf_token
        return await self._client.request(
            method,
            self._base_url + path,
            headers=headers,
            json=json,
            params=params,
        )

    async def _mutate(self, method: str, path: str, payload: dict[str, Any]) -> httpx.Response:
        if self._csrf_token is None:
            self._csrf_token = await self._fetch_csrf_token()

        response = await self._send(method, path, json=payload, csrf_token=self._csrf_token)
        if response.status_code == 403:
            # The token may be expired or single-use; refresh it once and retry.
            self._csrf_token = await self._fetch_csrf_token()
            response = await self._send(method, path, json=payload, csrf_token=self._csrf_token)
        self._raise_for_problem(response)
        return response

    async def _fetch_csrf_token(self) -> str:
        response = await self._send("GET", "/api/v1/auth/csrf")
        self._raise_for_problem(response)

        body = response.json()
        token = body.get("csrfToken") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise RuntimeError("CCH auth csrf returned unexpected data shape")
        return token

    def _raise_for_problem(self, response: httpx.Response) -> None:
        if response.is_success:
            return

        try:
            body = response.json()
        except ValueError:
            body = None

        if isinstance(body, dict):
            error_code = body.get("errorCode")
            detail = body.get("detail")
            raise CchApiError(
                f"CCH API request failed with HTTP {response.status_code}"
                f" {error_code or 'unknown error'}: {detail or ''}".rstrip(": "),
                status_code=response.status_code,
                error_code=error_code if isinstance(error_code, str) else None,
                detail=detail if isinstance(detail, str) else None,
            )

        raise CchApiError(
            f"CCH API request failed with HTTP {response.status_code}: {response.text[:200]}",
            status_code=response.status_code,
        )
