# Tavily Hub 搜索提供方实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 使用三个 `TAVILY_HUB_API_KEY` 环境变量通过 Tavily Hub 代理执行现有概念与五维证据搜索。

**架构：** 新增一个实现现有 `search(**kwargs)` 契约的 Hub HTTP 客户端。它按请求轮换 Key，发送网关所需的 Bearer 认证，并将 `{"code": 0, "data": {"ok": true, "data": {...}}}` 解包为现有调用方所需的 Tavily 响应对象。`ai_agent.py` 仅负责从 `.env` 读取 Key 并构造该客户端。

**技术栈：** Python 3、requests、pytest、python-dotenv。

---

### 任务 1：定义并验证 Hub 客户端契约

**文件：**
- 创建：`tavily_hub.py`
- 创建：`tests/test_tavily_hub.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_hub_client_rotates_keys_and_unwraps_search_response() -> None:
    client = TavilyHubClient(("hub-1", "hub-2", "hub-3"), request_func=fake_post)
    assert client.search(query="测试")["results"] == []
    assert client.search(query="测试二")["results"] == []
    assert authorization_headers == ["Bearer hub-1", "Bearer hub-2"]
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest tests/test_tavily_hub.py -q`

预期：FAIL，提示无法导入 `tavily_hub`。

- [ ] **步骤 3：编写最少实现代码**

```python
class TavilyHubClient:
    def search(self, **kwargs: object) -> dict[str, object]:
        key = self._next_key()
        response = self._request(HUB_SEARCH_URL, headers={"Authorization": f"Bearer {key}"}, json=kwargs, timeout=20)
        return _unwrap_response(response)
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest tests/test_tavily_hub.py -q`

预期：PASS。

### 任务 2：用 Hub Key 构造搜索客户端

**文件：**
- 修改：`ai_agent.py`
- 修改：`tests/test_ai_agent.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_build_tavily_client_reads_all_configured_hub_keys(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TAVILY_HUB_API_KEY1=one\nTAVILY_HUB_API_KEY3=three\n")
    client = _build_tavily_client(tmp_path)
    assert client.api_keys == ("one", "three")
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv\Scripts\python.exe -m pytest tests/test_ai_agent.py::test_build_tavily_client_reads_all_configured_hub_keys -q`

预期：FAIL，现有构造器仍要求 `TAVILY_API_KEY`。

- [ ] **步骤 3：编写最少实现代码**

```python
def _build_tavily_client(project_root: Path) -> TavilyHubClient:
    keys = tuple(value for index in range(1, 4) if (value := _load_optional_key(project_root, f"TAVILY_HUB_API_KEY{index}")))
    if not keys:
        raise RuntimeError(".env 中缺少 TAVILY_HUB_API_KEY1、TAVILY_HUB_API_KEY2 或 TAVILY_HUB_API_KEY3。")
    return TavilyHubClient(keys)
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv\Scripts\python.exe -m pytest tests/test_ai_agent.py::test_build_tavily_client_reads_all_configured_hub_keys -q`

预期：PASS。

### 任务 3：更新依赖和操作文档

**文件：**
- 修改：`requirements.txt`
- 修改：`README.md`

- [ ] **步骤 1：移除不再使用的官方 SDK 依赖并写明 Hub Key 配置**

```dotenv
DEEPSEEK_API_KEY=sk-...
TAVILY_HUB_API_KEY1=thb-...
TAVILY_HUB_API_KEY2=thb-...
TAVILY_HUB_API_KEY3=thb-...
```

- [ ] **步骤 2：运行相关测试**

运行：`.venv\Scripts\python.exe -m pytest tests/test_tavily_hub.py tests/test_ai_agent.py tests/test_concept_discovery.py tests/test_tavily_evidence.py -q`

预期：PASS，且不发起真实 Hub 请求。
