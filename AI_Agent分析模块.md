以下是完整的系统流程设计，包含每个环节的输入输出定义、Prompt模板和数据处理逻辑。

---

## 系统总览

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              系统全流程                                     │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  [步骤1] 获取概念板块              [步骤2] 多维度搜索           [步骤3] 综合评估 │
│                                                                             │
│  CSV(股票代码) ──→ DeepSeek ──→ 板块映射文件  ──→ Tavily ──→ 搜索缓存文件 ──→ DeepSeek ──→ 最终排名  │
│       (联网搜索)    (parquet/csv)   (5维度并行)    (parquet/csv)   (综合评分)    (前10名)  │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 步骤一：获取股票的概念板块（DeepSeek 联网搜索）

### 输入

`前 50 名（含所属行业）.csv` 文件，包含以下列（文件前两列）：

| 列名 | 类型 | 示例 | 说明 |
|:---|:---|:---|:---|
| `stock_code` | str | `"000066"` | 6位数字代码 |
| `stock_name` | str | `"中国长城"` | 股票名称 |

**CSV 示例：**
```csv
stock_code,stock_name
000066,中国长城
600519,贵州茅台
000001,平安银行
300750,宁德时代
```

### 核心逻辑

调用 DeepSeek API 时开启 `tools=[{"type": "web_search"}]`，让模型自行搜索并整理每只股票所属的**前5个主要概念板块**。需要注意的是，DeepSeek 的联网搜索默认返回标准格式结果，按照行业惯例，模型应优先筛选市场公认的核心标签。

### DeepSeek System Prompt（步骤一）

```
你是一名A股行业研究员。你的任务是为给定的股票列表，查询每只股票所属的**主要概念板块**（前5个）。

【概念板块定义】
- 概念板块是指基于"市场主题"或"炒作逻辑"划分的板块（如：信创、AI算力、国产芯片、央企改革、新能源车等）
- 不是行业板块（如：计算机、电子、食品饮料）
- 不是地域板块（如：广东板块、北京板块）

【搜索要求】
1. 对于每只股票，通过联网搜索查询其所属概念板块
2. 按该股票与概念的相关性从高到低排序，取前5个
3. 概念板块名称使用市场通用简称（如"信创"而非"信息技术应用创新"）
4. 如果某个概念板块包含该股票作为"核心成分股"或"龙头股"，标注星号(*)

【输出格式】
严格按照以下JSON格式输出，不要添加任何解释文字：
{
  "results": [
    {"code": "000066", "name": "中国长城", "concepts": ["信创*", "AI算力", "国产芯片", "央企改革", "网络安全"]},
    {"code": "600519", "name": "贵州茅台", "concepts": ["白酒", "超级品牌", "MSCI中国", "央企改革", "食品饮料"]}
  ]
}
```

### User Prompt 模板（步骤一）

```
以下是需要查询的股票列表（共{N}只），请查询每只股票所属的前5个概念板块：

{CSV内容粘贴到这里}

请严格按照JSON格式输出。
```

### 后处理逻辑

```python
import pandas as pd
import json
from datetime import datetime

def process_step1(deepseek_response_json: str, output_dir: str = "./data"):
    """
    处理步骤一的输出，保存为结构化文件
    """
    # 解析JSON
    data = json.loads(deepseek_response_json)
    results = data["results"]
    
    # 转换为DataFrame（展开概念板块为多行，方便后续关联）
    rows = []
    for item in results:
        for idx, concept in enumerate(item["concepts"], 1):
            # 处理星号标记
            is_core = "*" in concept
            concept_name = concept.replace("*", "")
            rows.append({
                "stock_code": item["code"],
                "stock_name": item["name"],
                "concept_rank": idx,
                "concept_name": concept_name,
                "is_core": is_core
            })
    
    df = pd.DataFrame(rows)
    
    # 同时保存一份汇总版本（每行一只股票，concepts列存储列表）
    summary = []
    for item in results:
        summary.append({
            "stock_code": item["code"],
            "stock_name": item["name"],
            "concepts": item["concepts"],
            "primary_concept": item["concepts"][0].replace("*", "") if item["concepts"] else None
        })
    df_summary = pd.DataFrame(summary)
    
    # 保存为Parquet（高效存储，保留类型）和CSV（便于查看）
    date_str = datetime.now().strftime("%Y%m%d")
    df.to_parquet(f"{output_dir}/step1_concepts_{date_str}.parquet")
    df_summary.to_csv(f"{output_dir}/step1_concepts_summary_{date_str}.csv", index=False)
    
    # 返回用于步骤二的板块映射字典
    board_mapping = dict(zip(df_summary["stock_code"], df_summary["primary_concept"]))
    return board_mapping, df_summary
```

### 输出文件

| 文件 | 格式 | 说明 |
|:---|:---|:---|
| `step1_concepts_{date}.parquet` | Parquet | 详细版（每只股票×每个概念一行） |
| `step1_concepts_summary_{date}.csv` | CSV | 汇总版（每只股票一行，含主概念） |

---

## 步骤二：多维度搜索（Tavily API）

### 输入

步骤一输出的 `step1_concepts_summary_{date}.csv`，以及原始股票列表。

### 核心逻辑

对每只股票执行**5维度并行搜索**，使用 Tavily API 的参数优化配置。其中 `board_name` 使用步骤一得出的 `primary_concept`。

### Python 实现

```python
import os
import time
import pandas as pd
from datetime import datetime
from tavily import TavilyClient

tavily = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])

def search_stock_dimensions(stock_code: str, stock_name: str, board_name: str):
    """
    对单只股票执行五维度并行搜索
    """
    queries = {
        "board_strength": f"{board_name} 板块 最新政策 主力资金 龙头股 2026",
        "stock_funds": f"{stock_name} {stock_code} 主力资金 北向资金 净流入",
        "stock_news": f"{stock_name} {stock_code} 最新公告 新闻 动态",
        "market_analysis": f"{stock_name} {stock_code} 券商 研报 评级 目标价",
        "stock_risk": f"{stock_name} {stock_code} 利空 减持 监管 警示"
    }
    
    results = {}
    for key, query in queries.items():
        try:
            response = tavily.search(
                query=query,
                topic="finance",
                search_depth="advanced",
                time_range="week",
                include_domains=[
                    "cninfo.com.cn",
                    "eastmoney.com",
                    "stcn.com",
                    "cs.com.cn",
                    "finance.sina.com.cn",
                    "cls.cn",
                    "jrj.com.cn"
                ],
                exclude_domains=["guba.eastmoney.com"],
                max_results=5
            )
            # 提取关键信息
            results[key] = [
                {
                    "title": r["title"],
                    "content": r["content"][:500],  # 截取前500字符
                    "url": r["url"]
                }
                for r in response["results"]
            ]
        except Exception as e:
            print(f"搜索失败 {stock_code} - {key}: {e}")
            results[key] = []
        
        time.sleep(0.5)  # 避免频率限制
    
    return results

def process_step2(stock_list: list, board_mapping: dict, output_dir: str = "./data"):
    """
    步骤二主流程：对每只股票执行搜索
    """
    all_results = []
    
    for stock in stock_list:
        code = stock["stock_code"]
        name = stock["stock_name"]
        board = board_mapping.get(code, "A股")
        
        print(f"正在搜索: {name}({code}) 所属板块: {board}")
        
        search_data = search_stock_dimensions(code, name, board)
        
        # 结构化存储
        record = {
            "stock_code": code,
            "stock_name": name,
            "primary_board": board,
            "search_time": datetime.now().isoformat(),
            "board_strength": json.dumps(search_data["board_strength"], ensure_ascii=False),
            "stock_funds": json.dumps(search_data["stock_funds"], ensure_ascii=False),
            "stock_news": json.dumps(search_data["stock_news"], ensure_ascii=False),
            "market_analysis": json.dumps(search_data["market_analysis"], ensure_ascii=False),
            "stock_risk": json.dumps(search_data["stock_risk"], ensure_ascii=False)
        }
        all_results.append(record)
        
        # 避免请求过密
        time.sleep(1)
    
    # 保存
    df = pd.DataFrame(all_results)
    date_str = datetime.now().strftime("%Y%m%d")
    df.to_parquet(f"{output_dir}/step2_search_results_{date_str}.parquet")
    df.to_csv(f"{output_dir}/step2_search_results_{date_str}.csv", index=False)
    
    return df
```

### 输出文件

| 文件 | 格式 | 说明 |
|:---|:---|:---|
| `step2_search_results_{date}.parquet` | Parquet | 所有股票的多维度搜索结果 |
| `step2_search_results_{date}.csv` | CSV | 同上（便于人工查看） |

---

## 步骤三：综合评估（DeepSeek 分析）

### 输入

步骤二输出的 `step2_search_results_{date}.parquet`。

### 核心逻辑

将步骤二的搜索结果拼装成压缩文本，调用 DeepSeek 进行综合评估，为每只股票输出评分，最终由汇总层选出前10名。

### DeepSeek System Prompt（步骤三）

```
你是一名A股事件驱动策略分析师。你的任务是对给定的股票搜索结果进行综合分析，输出评分和结论。

【分析原则】
1. 板块优先：板块热度的权重占60%，个股占40%
2. 证伪优先：先找反对证据（减持、监管问询、业绩下滑等）
3. 区分信号与噪音：普通中标（<1亿）视为噪音；巨额订单（占营收>10%）视为强信号
4. 板块梯队分析：板块内个股普涨视为"真看好"；仅龙头涨跟风不涨视为"假看好"

【评分标准】

个股被看好程度（1-10分）：
- 9-10：公司有重大利好（巨额订单/业绩暴增/重大突破）+ 主力资金大幅流入
- 7-8：有明确利好 + 资金温和流入
- 5-6：无明显利好也无明显利空
- 3-4：有利空消息（业绩下滑/减持）
- 1-2：重大利空（业绩暴雷/监管立案）

板块被看好程度（1-10分）：
- 9-10：板块处于市场热点前3 + 政策强催化 + 板块内涨停潮
- 7-8：板块处于热点前10 + 有政策/事件催化
- 5-6：板块随大盘波动，无明显催化
- 3-4：板块持续下跌或资金持续流出
- 1-2：板块有明显政策打压或行业利空

【输出格式】
严格按照以下JSON格式输出，不要添加任何解释文字：
{
  "results": [
    {
      "stock_code": "000066",
      "stock_name": "中国长城",
      "individual_score": 8,
      "individual_reason": "中标12亿大单，主力资金连续流入",
      "sector_score": 9,
      "sector_reason": "信创板块全线上涨，政策持续催化",
      "final_verdict": "看好",
      "key_risk": "扣非净利润仍亏损"
    }
  ]
}
```

### User Prompt 模板（步骤三）

```
以下是{日期}对{股票数量}只股票的搜索结果数据，请对每只股票进行分析评分：

{将步骤二的搜索结果压缩成以下格式}

【股票A：中国长城（000066）】
- 所属板块：信创
- 板块强度搜索结果：{board_strength的内容摘要}
- 个股资金流向：{stock_funds的内容摘要}
- 个股新闻动态：{stock_news的内容摘要}
- 机构研报观点：{market_analysis的内容摘要}
- 风险监控信息：{stock_risk的内容摘要}

【股票B：贵州茅台（600519）】
...（以此类推）

请严格按照JSON格式输出评分结果。
```

### 后处理逻辑

```python
def process_step3(deepseek_response_json: str, output_dir: str = "./data"):
    """
    处理步骤三的输出，计算最终综合得分并选出前10名
    """
    data = json.loads(deepseek_response_json)
    results = data["results"]
    
    # 计算综合得分：板块×0.6 + 个股×0.4
    for item in results:
        item["composite_score"] = round(
            item["sector_score"] * 0.6 + item["individual_score"] * 0.4,
            2
        )
    
    # 按综合得分排序
    sorted_results = sorted(results, key=lambda x: x["composite_score"], reverse=True)
    
    # 添加排名
    for idx, item in enumerate(sorted_results, 1):
        item["rank"] = idx
    
    # 提取前10名
    top10 = sorted_results[:10]
    
    # 保存全部结果
    df = pd.DataFrame(sorted_results)
    df.to_csv(f"{output_dir}/all_rankings.csv", index=False)
    
    # 保存前10名
    df_top10 = pd.DataFrame(top10)
    df_top10.to_csv(f"{output_dir}/top10_recommendations.csv", index=False)
    
    # 打印前10名
    print("=" * 50)
    print("今日推荐前10名")
    print("=" * 50)
    for item in top10:
        print(f"{item['rank']}. {item['stock_name']}({item['stock_code']})")
        print(f"   综合得分: {item['composite_score']} | 个股: {item['individual_score']} | 板块: {item['sector_score']}")
        print(f"   理由: {item['individual_reason']} | 板块理由: {item['sector_reason']}")
        print(f"   风险: {item.get('key_risk', '无')}")
        print("-" * 30)
    
    return df, df_top10
```

### 输出文件

| 文件 | 格式 | 说明 |
|:---|:---|:---|
| `all_rankings.csv` | CSV | 全部股票的完整排名和评分 |
| `top10_recommendations.csv` | CSV | **最终推荐前10名** |

---

## 数据流转图

```
                    ┌──────────────────────────────────────────────────────────────────┐
                    │                    数据在各步骤间的流转                          │
                    └──────────────────────────────────────────────────────────────────┘

┌──────────────────┐      ┌──────────────────┐      ┌──────────────────┐      ┌──────────────────┐
│   candidates.csv  │ ──→ │  Step 1 输出     │ ──→ │  Step 2 输出     │ ──→ │  Step 3 输出     │
│                   │      │                  │      │                  │      │                  │
│ stock_code        │      │ stock_code       │      │ stock_code       │      │ stock_code       │
│ stock_name        │      │ stock_name       │      │ stock_name       │      │ stock_name       │
└──────────────────┘      │ concept_rank     │      │ primary_board    │      │ individual_score │
                          │ concept_name     │      │ board_strength   │      │ sector_score     │
                          │ is_core          │      │ stock_funds      │      │ final_verdict    │
                          └──────────────────┘      │ stock_news       │      │ composite_score  │
                                                    │ market_analysis  │      │ rank             │
                                                    │ stock_risk       │      └──────────────────┘
                                                    └──────────────────┘
```

---

## 环境变量配置

```bash
# .env
DEEPSEEK_API_KEY="sk-xxxxx"
TAVILY_API_KEY="tvly-xxxxx"
```

---

## 运行命令

```bash
python ai_agent.py
```

### 主函数伪代码

```python
def main():
    # 1. 读取CSV
    stocks = pd.read_csv("candidates.csv")
    
    # 2. 步骤一：获取概念板块
    board_mapping, _ = step1_get_concepts(stocks)
    
    # 3. 步骤二：Tavily搜索
    search_results = step2_search(stocks, board_mapping)
    
    # 4. 步骤三：综合评估
    final_df, top10_df = step3_evaluate(search_results)
    
    # 5. 输出推荐
    print("今日推荐：")
    print(top10_df[["rank", "stock_name", "composite_score", "final_verdict"]])
```

---

