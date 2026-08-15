"""Tavily Hub search client compatible with the existing search protocol."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from threading import Lock
from typing import Any

import requests


HUB_SEARCH_URL = "https://tavily.sharyuke.com/api/proxy/search"
DEFAULT_TIMEOUT_SECONDS = 20


class TavilyHubRequestError(RuntimeError):
    """A Tavily Hub request failed with a status usable by retry logic."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class TavilyHubClient:
    """Round-robin Tavily Hub keys and unwrap the gateway response envelope."""

    def __init__(
        self,
        api_keys: tuple[str, ...],
        *,
        request_func: Callable[..., object] = requests.post,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        keys = tuple(key.strip() for key in api_keys if key.strip())
        if not keys:
            raise ValueError("至少需要一个 Tavily Hub API Key。")
        self.api_keys = keys
        self._request = request_func
        self._timeout_seconds = timeout_seconds
        self._key_index = 0
        self._key_lock = Lock()

    def search(self, **kwargs: object) -> dict[str, object]:
        key = self._next_key()
        try:
            response = self._request(
                HUB_SEARCH_URL,
                headers={"Authorization": f"Bearer {key}"},
                json=dict(kwargs),
                timeout=self._timeout_seconds,
            )
        except requests.RequestException as error:
            raise TavilyHubRequestError(
                f"Tavily Hub 请求失败：{error}", status_code=503
            ) from error
        return _unwrap_response(response)

    def _next_key(self) -> str:
        with self._key_lock:
            key = self.api_keys[self._key_index]
            self._key_index = (self._key_index + 1) % len(self.api_keys)
            return key


def _unwrap_response(response: object) -> dict[str, object]:
    status_code = getattr(response, "status_code", None)
    if not isinstance(status_code, int):
        raise TavilyHubRequestError("Tavily Hub 响应缺少 HTTP 状态码。", status_code=502)
    try:
        payload = getattr(response, "json")()
    except (AttributeError, TypeError, ValueError) as error:
        raise TavilyHubRequestError(
            "Tavily Hub 返回了无效 JSON。", status_code=status_code
        ) from error
    if not isinstance(payload, Mapping):
        raise TavilyHubRequestError("Tavily Hub 返回了非对象 JSON。", status_code=status_code)

    gateway_code = payload.get("code")
    message = str(payload.get("message") or "未知网关错误")
    error_status = gateway_code if isinstance(gateway_code, int) else status_code
    if status_code < 200 or status_code >= 300 or gateway_code != 0:
        raise TavilyHubRequestError(f"Tavily Hub 请求失败：{message}", status_code=error_status)

    envelope = payload.get("data")
    if not isinstance(envelope, Mapping) or envelope.get("ok") is not True:
        raise TavilyHubRequestError(
            f"Tavily Hub 请求未成功：{message}", status_code=error_status
        )
    data = envelope.get("data")
    if not isinstance(data, Mapping):
        raise TavilyHubRequestError("Tavily Hub 响应缺少搜索结果。", status_code=502)
    return {str(key): value for key, value in data.items()}
