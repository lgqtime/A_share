from __future__ import annotations

from datetime import date
import json
from typing import Protocol

from ai_agent_io import CacheStore
from ai_agent_models import Candidate, Concept, ConceptResult


PROMPT_VERSION = "concept-v2"
SYSTEM_PROMPT = """你是一名 A 股行业研究员。
仅返回市场通用的主题概念简称，排除行业、地域、指数、宽泛市场标签和非主题性描述。
按关联度排序，最多五个概念。核心股判断必须使用 is_core 布尔字段，概念名称不能使用星号。
严格返回 JSON，不要添加解释。"""


class ConceptResponseError(ValueError):
    """Raised when a concept response does not match the agreed schema."""


class ConceptClient(Protocol):
    def complete(
        self, *, system_prompt: str, user_prompt: str, tools: list[dict[str, str]]
    ) -> str: ...


class ConceptDiscovery:
    def __init__(
        self,
        client: ConceptClient,
        cache: CacheStore,
        *,
        analysis_date: date,
        model_version: str,
    ) -> None:
        self.client = client
        self.cache = cache
        self.analysis_date = analysis_date
        self.model_version = model_version

    def discover(self, candidate: Candidate) -> ConceptResult:
        cached = self.cache.read_concept(
            self.analysis_date,
            candidate.stock_code,
            self.model_version,
            PROMPT_VERSION,
        )
        if cached is not None:
            return cached

        raw_response = self.client.complete(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=(
                "查询以下股票的市场主题概念。只输出一个 JSON 对象，不要 Markdown。"
                f"股票代码：{candidate.stock_code}\n股票名称：{candidate.stock_name}\n"
                "JSON 架构必须为："
                f'{{"stock_code":"{candidate.stock_code}","stock_name":"{candidate.stock_name}",'
                '"concepts":[{"concept_name":"主题简称","concept_rank":1,"is_core":false}]}。'
                "没有有效概念时 concepts 必须是空列表。"
            ),
            tools=[{"type": "web_search"}],
        )
        result = parse_concept_response(raw_response, candidate)
        result = ConceptResult(
            stock_code=result.stock_code,
            stock_name=result.stock_name,
            concepts=result.concepts,
            raw_response=raw_response,
        )
        self.cache.write_concept(
            self.analysis_date,
            result,
            self.model_version,
            PROMPT_VERSION,
            raw_response,
        )
        return result


def verify_web_search_capability(client: ConceptClient) -> None:
    response = client.complete(
        system_prompt="验证联网搜索工具可用性。只返回一个 JSON 对象。",
        user_prompt='请调用联网搜索工具，并返回 JSON：{"status":"ok"}。',
        tools=[{"type": "web_search"}],
    )
    if not isinstance(response, str) or not response.strip():
        raise RuntimeError("DeepSeek 联网搜索能力预检返回空响应")


def parse_concept_response(raw_response: str, candidate: Candidate) -> ConceptResult:
    try:
        payload = json.loads(raw_response)
    except json.JSONDecodeError as error:
        raise ConceptResponseError("概念响应不是有效 JSON") from error

    if not isinstance(payload, dict):
        raise ConceptResponseError("概念响应必须是对象")
    if payload.get("stock_code") != candidate.stock_code:
        raise ConceptResponseError("概念响应的股票身份不匹配")
    raw_concepts = payload.get("concepts")
    if not isinstance(raw_concepts, list) or len(raw_concepts) > 5:
        raise ConceptResponseError("概念列表必须是至多五项的列表")

    concepts: list[Concept] = []
    for expected_rank, item in enumerate(raw_concepts, start=1):
        if not isinstance(item, dict):
            raise ConceptResponseError("概念项必须是对象")
        concept_name = item.get("concept_name")
        if not isinstance(concept_name, str) or not concept_name.strip():
            raise ConceptResponseError("概念名称不能为空")
        if "*" in concept_name:
            raise ConceptResponseError("概念名称不能包含星号")
        if item.get("concept_rank") != expected_rank:
            raise ConceptResponseError("概念排名必须连续且从一开始")
        if not isinstance(item.get("is_core"), bool):
            raise ConceptResponseError("核心标记必须是布尔值")
        concepts.append(
            Concept(
                concept_name=concept_name.strip(),
                concept_rank=expected_rank,
                is_core=item["is_core"],
            )
        )
    return ConceptResult(candidate.stock_code, candidate.stock_name, tuple(concepts))
