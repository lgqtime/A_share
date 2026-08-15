from __future__ import annotations

import pytest

from tavily_hub import TavilyHubClient, TavilyHubRequestError


class FakeResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


def test_hub_client_rotates_keys_and_unwraps_search_response() -> None:
    calls: list[dict[str, object]] = []

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        return FakeResponse(
            200,
            {
                "code": 0,
                "message": "ok",
                "data": {"ok": True, "data": {"results": [], "query": "测试"}},
            },
        )

    client = TavilyHubClient(
        tuple(f"hub-key-{index}" for index in range(1, 7)), request_func=fake_post
    )

    first = client.search(query="测试", max_results=5, topic="finance")
    second = client.search(query="测试二", search_depth="advanced")
    for index in range(3, 7):
        client.search(query=f"测试{index}")

    assert first == {"results": [], "query": "测试"}
    assert second == {"results": [], "query": "测试"}
    assert [call["headers"] for call in calls] == [
        {"Authorization": "Bearer hub-key-1"},
        {"Authorization": "Bearer hub-key-2"},
        {"Authorization": "Bearer hub-key-3"},
        {"Authorization": "Bearer hub-key-4"},
        {"Authorization": "Bearer hub-key-5"},
        {"Authorization": "Bearer hub-key-6"},
    ]
    assert calls[0]["url"] == "https://tavily.sharyuke.com/api/proxy/search"
    assert calls[0]["json"] == {"query": "测试", "max_results": 5, "topic": "finance"}
    assert calls[1]["json"] == {"query": "测试二", "search_depth": "advanced"}


def test_hub_client_exposes_retryable_gateway_status() -> None:
    client = TavilyHubClient(
        ("hub-key",),
        request_func=lambda url, **kwargs: FakeResponse(
            429,
            {"code": 429, "message": "超过速率限制", "data": {"ok": False}},
        ),
    )

    with pytest.raises(TavilyHubRequestError) as error:
        client.search(query="测试")

    assert error.value.status_code == 429
    assert "超过速率限制" in str(error.value)
