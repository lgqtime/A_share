# A 股联网证据分析模块实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 `executing-plans` 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 实现独立的 A 股联网证据分析命令行工具，使用 DeepSeek 概念识别、Tavily 五维检索和 DeepSeek 逐股评分，生成可审计的固定前十结果。

**架构：** `ai_agent_models.py` 定义无网络的数据契约；`ai_agent_io.py` 处理固定候选输入、当天缓存和运行产物；DeepSeek 概念识别、Tavily 检索和 DeepSeek 评分分别封装为可注入客户端；`ranking.py` 在程序侧执行证据门槛、风险封顶和确定性排序；`ai_agent.py` 只负责编排和启动前检查。

**技术栈：** Python 3.10+、pandas、OpenAI SDK（DeepSeek 兼容客户端）、Tavily Python SDK、python-dotenv、unittest、pytest。

---

## 文件结构

- 创建：`ai_agent_models.py`，候选、概念、证据、评分和运行状态的数据类。
- 创建：`ai_agent_io.py`，固定 CSV 读取、缓存键、JSONL/CSV/运行清单写入。
- 创建：`concept_discovery.py`，DeepSeek 联网概念识别与严格 JSON 校验。
- 创建：`tavily_evidence.py`，五维 Tavily 查询、并发、重试与证据标准化。
- 创建：`evidence_evaluator.py`，DeepSeek 逐股证据评分、一次修复和引用校验。
- 创建：`ranking.py`，门槛、风险封顶、等级和排序。
- 创建：`ai_agent.py`，无参数命令行编排与密钥/能力检查。
- 创建：`tests/test_ai_agent_models.py`、`tests/test_ai_agent_io.py`、`tests/test_concept_discovery.py`、`tests/test_tavily_evidence.py`、`tests/test_evidence_evaluator.py`、`tests/test_ranking.py`、`tests/test_ai_agent.py`。
- 修改：`requirements.txt`、`README.md`。
- 删除：`data_fetcher.py`、`tests/test_data_fetcher.py`、`tests/test_ai_analyzer.py`、`tests/test_a_share_agent.py`、`docs/superpowers/plans/2026-08-14-ai-agent-analysis.md`。

## 任务 1：移除已排除的旧模块并声明依赖

**文件：**
- 删除：`data_fetcher.py`
- 删除：`tests/test_data_fetcher.py`
- 删除：`tests/test_ai_analyzer.py`
- 删除：`tests/test_a_share_agent.py`
- 删除：`docs/superpowers/plans/2026-08-14-ai-agent-analysis.md`
- 修改：`requirements.txt`
- 修改：`README.md`

- [x] **步骤 1：删除旧 AKShare 和单次分析路线的源文件、测试和计划。**

保留项目其他 AKShare 业务及其依赖，不删除 `pyproject.toml` 中的既有依赖。

- [x] **步骤 2：将 Tavily SDK 加入独立脚本依赖。**

```text
tavily-python>=0.5
```

- [x] **步骤 3：将 README 的旧 “独立 A 股 AI 分析” 小节替换为新命令和边界。**

```markdown
## 独立 A 股联网证据分析

`python ai_agent.py` 固定读取 `前 50 名（含所属行业）.csv` 的 `股票代码`、`股票名称`，并将当天的可审计结果写入 `ai_agent_outputs/`。该工具不接入交易或定时任务。
```

- [ ] **步骤 4：运行完整测试，确认旧模块收集错误已经消失。**

运行：`.venv\Scripts\python.exe -m pytest -q`

预期：不再出现 `ModuleNotFoundError: A_Share_Agent` 或 `ModuleNotFoundError: ai_analyzer`；新模块测试尚未创建。

## 任务 2：数据契约、固定候选输入和运行产物

**文件：**
- 创建：`tests/test_ai_agent_models.py`
- 创建：`tests/test_ai_agent_io.py`
- 创建：`ai_agent_models.py`
- 创建：`ai_agent_io.py`

- [x] **步骤 1：编写失败的数据契约和输入测试。**

```python
def test_load_candidates_reads_every_valid_unique_row_from_fixed_file(tmp_path: Path) -> None:
    source = tmp_path / "前 50 名（含所属行业）.csv"
    pd.DataFrame({"股票代码": [66, "600519", 66, "bad"], "股票名称": ["甲", "乙", "重复", "无效"]}).to_csv(
        source, index=False, encoding="utf-8-sig"
    )
    candidates, ignored = load_candidates(source)
    assert candidates == [Candidate("000066", "甲"), Candidate("600519", "乙")]
    assert ignored == [{"row": 4, "reason": "重复股票代码: 000066"}, {"row": 5, "reason": "无效股票代码"}]

def test_output_writer_creates_independent_run_directory_and_manifest(tmp_path: Path) -> None:
    run = RunPaths.create(tmp_path, date(2026, 8, 14), "153000")
    write_manifest(run, {"candidate_count": 2, "candidate_shortfall": True})
    assert run.manifest.exists()
    assert json.loads(run.manifest.read_text(encoding="utf-8"))["candidate_shortfall"] is True
```

- [x] **步骤 2：运行测试确认失败。**

运行：`.venv\Scripts\python.exe -m pytest tests/test_ai_agent_models.py tests/test_ai_agent_io.py -q`

预期：收集失败，提示缺少 `ai_agent_models` 或 `ai_agent_io`。

- [x] **步骤 3：实现最小数据模型和输入/产物工具。**

```python
@dataclass(frozen=True)
class Candidate:
    stock_code: str
    stock_name: str

@dataclass(frozen=True)
class RunPaths:
    root: Path
    manifest: Path

    @classmethod
    def create(cls, project_root: Path, analysis_date: date, run_id: str) -> "RunPaths":
        root = project_root / "ai_agent_outputs" / analysis_date.strftime("%Y%m%d") / run_id
        root.mkdir(parents=True, exist_ok=False)
        return cls(root=root, manifest=root / "run_manifest.json")
```

`load_candidates` 必须要求中文列名，保留首个重复，返回候选和带行号的忽略记录；`write_manifest` 使用 UTF-8 JSON。

- [x] **步骤 4：运行测试确认通过。**

运行：`.venv\Scripts\python.exe -m pytest tests/test_ai_agent_models.py tests/test_ai_agent_io.py -q`

预期：所有测试通过。

## 任务 3：DeepSeek 联网概念识别和缓存

**文件：**
- 创建：`tests/test_concept_discovery.py`
- 创建：`concept_discovery.py`
- 修改：`ai_agent_models.py`
- 修改：`ai_agent_io.py`

- [x] **步骤 1：编写失败的概念结果、空概念和缓存测试。**

```python
def test_discover_returns_ranked_concepts_without_marker_characters(tmp_path: Path) -> None:
    client = FakeConceptClient('{"stock_code":"000066","stock_name":"中国长城","concepts":[{"concept_name":"信创","concept_rank":1,"is_core":true}]}')
    service = ConceptDiscovery(client, CacheStore(tmp_path), today_func=lambda: date(2026, 8, 14))
    result = service.discover(Candidate("000066", "中国长城"))
    assert result.primary_concept == "信创"
    assert result.concepts[0].is_core is True

def test_discover_uses_same_day_successful_cache(tmp_path: Path) -> None:
    client = FakeConceptClient('{"stock_code":"000066","stock_name":"中国长城","concepts":[]}')
    service = ConceptDiscovery(client, CacheStore(tmp_path), today_func=lambda: date(2026, 8, 14))
    service.discover(Candidate("000066", "中国长城"))
    service.discover(Candidate("000066", "中国长城"))
    assert client.calls == 1
```

- [x] **步骤 2：运行测试确认失败。**

运行：`.venv\Scripts\python.exe -m pytest tests/test_concept_discovery.py -q`

预期：收集失败，提示缺少 `concept_discovery`。

- [x] **步骤 3：实现概念客户端协议、严格校验和成功缓存。**

```python
class ConceptClient(Protocol):
    def complete(self, *, system_prompt: str, user_prompt: str, tools: list[dict[str, str]]) -> str: ...

class ConceptDiscovery:
    def discover(self, candidate: Candidate) -> ConceptResult:
        cached = self.cache.read_concept(candidate.stock_code, self.analysis_date, self.model_version, PROMPT_VERSION)
        if cached is not None:
            return cached
        response = self.client.complete(system_prompt=SYSTEM_PROMPT, user_prompt=build_prompt(candidate), tools=[{"type": "web_search"}])
        result = parse_concept_response(response, candidate)
        self.cache.write_concept(result, raw_response=response)
        return result
```

校验概念数不超过五、名次连续、概念名非空且不含 `*`；失败结果不写入成功缓存。

- [x] **步骤 4：运行测试确认通过。**

运行：`.venv\Scripts\python.exe -m pytest tests/test_concept_discovery.py -q`

预期：所有测试通过。

## 任务 4：Tavily 五维检索、状态和证据标准化

**文件：**
- 创建：`tests/test_tavily_evidence.py`
- 创建：`tavily_evidence.py`
- 修改：`ai_agent_models.py`
- 修改：`ai_agent_io.py`

- [x] **步骤 1：编写失败的五维、无主概念和部分失败测试。**

```python
def test_collect_skips_board_query_without_primary_concept() -> None:
    client = FakeTavilyClient()
    bundle = TavilyEvidenceCollector(client, max_workers=5, sleep_func=lambda _: None).collect(
        Candidate("000066", "中国长城"), primary_concept=None, analysis_year=2026
    )
    assert bundle.statuses["board_strength"] == "skipped"
    assert set(client.queries) == {"stock_funds", "stock_news", "market_analysis", "stock_risk"}

def test_collect_keeps_failed_dimension_without_dropping_other_evidence() -> None:
    client = FakeTavilyClient(fail_dimensions={"stock_risk"})
    bundle = TavilyEvidenceCollector(client, max_workers=5, sleep_func=lambda _: None).collect(
        Candidate("000066", "中国长城"), primary_concept="信创", analysis_year=2026
    )
    assert bundle.statuses["stock_risk"] == "failed"
    assert bundle.statuses["stock_news"] == "success"
```

- [x] **步骤 2：运行测试确认失败。**

运行：`.venv\Scripts\python.exe -m pytest tests/test_tavily_evidence.py -q`

预期：收集失败，提示缺少 `tavily_evidence`。

- [x] **步骤 3：实现查询配置、线程并发、有限重试和标准化。**

```python
DIMENSIONS = ("board_strength", "stock_funds", "stock_news", "market_analysis", "stock_risk")

def collect(self, candidate: Candidate, primary_concept: str | None, analysis_year: int) -> EvidenceBundle:
    jobs = [dimension for dimension in DIMENSIONS if dimension != "board_strength" or primary_concept]
    with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
        completed = {name: future.result() for name, future in ((name, executor.submit(self._search, name, candidate, primary_concept, analysis_year)) for name in jobs)}
    completed["board_strength"] = DimensionResult.skipped() if not primary_concept else completed["board_strength"]
    return EvidenceBundle(candidate.stock_code, completed)
```

每项结果包含稳定 `evidence_id`、查询、标题、摘要、URL、供应商给出的发布时间和中国时区抓取时间。仅超时、连接错误、429、5xx 触发有限重试。

- [x] **步骤 4：运行测试确认通过。**

运行：`.venv\Scripts\python.exe -m pytest tests/test_tavily_evidence.py -q`

预期：所有测试通过。

## 任务 5：逐股评分、引用校验和确定性排序

**文件：**
- 创建：`tests/test_evidence_evaluator.py`
- 创建：`tests/test_ranking.py`
- 创建：`evidence_evaluator.py`
- 创建：`ranking.py`

- [x] **步骤 1：编写失败的评分引用、一次修复和风险门槛测试。**

```python
def test_evaluator_repairs_invalid_evidence_reference_once() -> None:
    evaluator = EvidenceEvaluator(FakeEvaluatorClient([invalid_reference_json(), valid_json()]))
    result = evaluator.evaluate(sample_candidate(), sample_bundle())
    assert result.analysis_status == "已评分"
    assert evaluator.client.calls == 2

def test_rank_caps_material_risk_and_still_returns_five_rows() -> None:
    rows = [sample_score(f"0000{i:02d}", risk_level="重大" if i == 1 else "无") for i in range(1, 7)]
    frame = rank_results(rows)
    assert frame.loc[0, "最终排序分"] <= 4
    assert len(top_rows(frame)) == 10
```

- [x] **步骤 2：运行测试确认失败。**

运行：`.venv\Scripts\python.exe -m pytest tests/test_evidence_evaluator.py tests/test_ranking.py -q`

预期：收集失败，提示缺少 `evidence_evaluator` 或 `ranking`。

- [x] **步骤 3：实现严格评分响应、一次修复和排序规则。**

```python
def ranking_score(result: ScoreResult) -> float:
    base = round(result.sector_score * 0.6 + result.individual_score * 0.4, 2)
    if result.analysis_status == "分析失败":
        return 0.0
    if result.risk_level == "重大" and result.risk_evidence_ids:
        return min(base, 4.0)
    if not result.evidence_gate_passed:
        return min(base, 6.0)
    return base
```

评估器验证候选身份、1 至 10 整数分数、结论和风险枚举、以及全部引用证据 ID；第二次响应仍无效时创建零分“分析失败”结果。排序依次使用最终分、证据门槛、风险等级、板块分、个股分和代码。

- [x] **步骤 4：运行测试确认通过。**

运行：`.venv\Scripts\python.exe -m pytest tests/test_evidence_evaluator.py tests/test_ranking.py -q`

预期：所有测试通过。

## 任务 6：无参数编排、运行清单和端到端模拟

**文件：**
- 创建：`tests/test_ai_agent.py`
- 创建：`ai_agent.py`
- 修改：`ai_agent_io.py`

- [x] **步骤 1：编写失败的端到端编排测试。**

```python
def test_run_processes_all_candidates_and_writes_top_five(tmp_path: Path) -> None:
    write_candidates(tmp_path / "前 50 名（含所属行业）.csv", count=6)
    output = run(
        project_root=tmp_path,
        concept_factory=lambda _: FakeConceptDiscovery(),
        evidence_factory=lambda _: FakeEvidenceCollector(),
        evaluator_factory=lambda _: FakeEvaluator(),
        now_func=lambda: datetime(2026, 8, 14, 15, 30, tzinfo=CHINA_TZ),
    )
    top10 = pd.read_csv(output.top10_csv, dtype={"股票代码": "string"})
    assert len(top10) == 10
    assert output.manifest.exists()
```

- [x] **步骤 2：运行测试确认失败。**

运行：`.venv\Scripts\python.exe -m pytest tests/test_ai_agent.py -q`

预期：收集失败，提示缺少 `ai_agent`。

- [x] **步骤 3：实现无参数 `run` 和命令行入口。**

```python
def run(project_root: Path | None = None, *, concept_factory=ConceptDiscovery.from_environment, evidence_factory=TavilyEvidenceCollector.from_environment, evaluator_factory=EvidenceEvaluator.from_environment, now_func=china_now) -> RunPaths:
    root = project_root or Path(__file__).resolve().parent
    now = now_func()
    candidates, ignored = load_candidates(root / CANDIDATE_FILE_NAME)
    paths = RunPaths.create(root, now.date(), now.strftime("%H%M%S"))
    concepts = concept_factory(root).discover_all(candidates)
    bundles = evidence_factory(root).collect_all(candidates, concepts, now.year)
    scores = evaluator_factory(root).evaluate_all(candidates, concepts, bundles)
    rankings = rank_results(scores, bundles)
    write_run_outputs(paths, candidates, concepts, bundles, rankings, ignored)
    return paths
```

入口在实际调用前检查两个环境变量和 DeepSeek 联网能力；不会接入任何调度或交易模块。

- [x] **步骤 4：运行新模块完整测试。**

运行：`.venv\Scripts\python.exe -m pytest tests/test_ai_agent_models.py tests/test_ai_agent_io.py tests/test_concept_discovery.py tests/test_tavily_evidence.py tests/test_evidence_evaluator.py tests/test_ranking.py tests/test_ai_agent.py -q`

预期：所有新模块测试通过。

- [ ] **步骤 5：运行完整回归。**

运行：`.venv\Scripts\python.exe -m pytest -q`

预期：所有测试通过；不运行真实联网请求。

## 自检

- 固定中文候选文件、全行处理、当天实时限制、DeepSeek 联网概念识别、Tavily 五维查询、证据 ID、一次修复、门槛、风险封顶、固定前十、缓存、产物、密钥保护和测试验收均对应至少一个任务。
- 所有网络交互均由构造函数或工厂注入，以便测试使用替身客户端。
- 当前工作区包含用户的无关改动；计划不要求暂存、提交、还原或修改这些文件。
