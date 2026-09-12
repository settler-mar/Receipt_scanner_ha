"""Общий асинхронный HTTP-клиент для адаптеров магазинов.

Интерфейс намеренно похож на используемую в проекте часть ``httpx.AsyncClient``,
чтобы адаптерам не приходилось знать о деталях ``curl_cffi``.
"""
from __future__ import annotations

import logging
from typing import Any

from curl_cffi.const import CurlHttpVersion
from curl_cffi.requests import AsyncSession, Response
from curl_cffi.requests.exceptions import RequestException, Timeout

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20.0

DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

__all__ = ["AsyncClient", "Response", "RequestException", "Timeout"]


class AsyncClient:
    """Небольшая обёртка над ``AsyncSession`` с привычным API ``httpx``.

        async with AsyncClient(timeout=20, headers=headers) as client:
            resp = await client.post(url, json={...})
    """

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        base_url: str = "",
        http2: bool = True,
    ) -> None:
        session_headers = {**DEFAULT_HEADERS, **(headers or {})}

        self._session = AsyncSession(
            timeout=timeout,
            headers=session_headers,
            cookies=cookies,
            base_url=base_url,
            http_version=CurlHttpVersion.V2TLS if http2 else CurlHttpVersion.V1_1,
            impersonate="chrome120",
        )

    async def __aenter__(self) -> AsyncClient:
        await self._session.__aenter__()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self._session.__aexit__(*exc_info)

    @property
    def cookies(self) -> dict[str, str]:
        return dict(self._session.cookies)

    async def warmup(
        self,
        url: str,
        *,
        follow_redirects: bool = True,
        **kwargs: Any,
    ) -> Response:
        """Открыть начальную страницу и сохранить выданные сервером cookie."""
        return await self._request(
            "GET",
            url,
            follow_redirects=follow_redirects,
            **kwargs,
        )

    async def get(self, url: str, **kwargs: Any) -> Response:
        return await self._request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> Response:
        return await self._request("POST", url, **kwargs)

    async def request(self, method: str, url: str, **kwargs: Any) -> Response:
        return await self._request(method, url, **kwargs)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        follow_redirects: bool | None = None,
        **kwargs: Any,
    ) -> Response:
        # В curl_cffi тот же параметр называется allow_redirects.
        if follow_redirects is not None:
            kwargs["allow_redirects"] = follow_redirects
        try:
            return await self._session.request(method, url, **kwargs)
        except Timeout:
            logger.error("HTTP %s %s timed out (timeout=%s)", method, url, self._session.timeout)
            raise
        except RequestException:
            logger.exception("HTTP %s %s failed", method, url)
            raise
