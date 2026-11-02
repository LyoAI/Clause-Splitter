# 单条外规内化分析 · 接口文档

模块:`clause_analysis`(算法侧,只处理**一条已拆好的条款**)。工程侧负责遍历条款、调用本模块、补齐文档级字段并回调 ESG。

## 1. 调用方式(进程内 Python)

```python
from clause_analysis import ClauseAnalyzer, ClauseInput

analyzer = ClauseAnalyzer.from_config()          # 启动时构建一次,可复用
for clause in clauses:                           # 工程的 for 循环
    res = await analyzer.analyze(ClauseInput(
        document_name="网络数据安全风险评估办法",
        clause_number="第十二条",
        section="第三章 风险评估内容",            # 无章节传 ""
        text="网络运营者应当……",
    ))
    element = res.to_esg()                       # data[] 元素中的算法侧字段
```

- `analyze` 是唯一对外方法;无状态,每次调用独立。
- 并发由调用方控制。内网 LLM 只有 2 个并发槽,并行度请 ≤ 2。
- 离线冒烟(真实 LLM + Zeus):`uv run python scripts/analyze_clause.py examples/clause_input.json`,加 `--raw` 输出完整结果。

## 2. 输入 `ClauseInput`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `text` | str | 是 | 条款原文,非空 |
| `document_name` | str | 否 | 外规名称(后端 docId 对应的法规名) |
| `clause_number` | str | 否 | 条款号,如 `第一条` / `Article 1` |
| `section` | str | 否 | 所属章节路径,无则 `""` |

输入只需条款原文与元信息;不需要、也不接受五元组等预拆分字段。

## 3. 输出 `ClauseResult` → `to_esg()`

`to_esg()` 返回 ESG 回调 `data[]` 的一个元素中**算法侧**的字段(字段名与 `assets/callback-contract.md` 一致):

| 字段 | 说明 |
|---|---|
| `termName` | 条款号 |
| `internalize` | `需要内化` / `无需内化` |
| `internalizeReason` | 内化判定依据 |
| `coverageStatus` | `完全覆盖` / `部分覆盖` / `缺失`;**无需内化时为空串**(未执行分析) |
| `analysis` | 覆盖分析;无需内化时为空串 |
| `differenceAnalysis` | 差异说明(仅部分覆盖) |
| `coreRequirements` | 核心监管要求 |
| `remedySuggestion` | 整改建议 |
| `relationTerms` | 采纳的内规条款数组,无则 `[]` |

`relationTerms` 元素:

```json
{
  "internalClauseId": "数据安全管理办法#2",
  "docName": "数据安全管理办法",
  "clauseName": "第二条",
  "clauseContent": "……"
}
```

- `internalClauseId` 始终保留;`docName` / `clauseName` / `clauseContent` 仅在本次检索缓存中能查到时回填,否则为空串。
- 覆盖结论按模型原样输出,引用 id 不影响 `coverageStatus`。

工程侧需补齐的字段(本模块不产出):`chapter`、`orderNum`、`termId`、`termContent`、`termKeyword`,以及文档级字段。

## 4. 错误语义

| 情况 | 行为 |
|---|---|
| 内化判定 LLM 超时/失败/输出无法解析 | 按**需要内化**处理,`internalizeReason` 注明原因,继续比对 |
| 单条比对超时(`EPISODE_TIMEOUT_S`)或执行异常 | 返回 `coverageStatus=缺失`,`analysis` 注明"需人工复核",不抛出 |
| Zeus 未配置 | 抛出 `RagNotConfigured` |
| Zeus 鉴权失败 | 抛出 `RagAuthError` |
| Zeus 检索异常(不可达 / 5xx) | 失败的检索不中断比对;若最终判定为缺失且出现过检索失败,降级为 `缺失` + 需人工复核,不抛出 |

抛出的两类是部署问题,调用方应中止整批。注意:单条降级时 `缺失` 与 `需人工复核` 仅靠 `analysis` 文案区分。

## 5. 配置(环境变量,`clause_analysis/config.py`)

| 变量 | 默认 | 说明 |
|---|---|---|
| `LLM_PROVIDER` / `LLM_MODEL_NAME` | `openai` / `qwen3.8-27b` | 对应 `config/llm/<model>.yaml` 的 section |
| `LLM_TIMEOUT_S` | 180 | 单次 LLM 请求超时 |
| `EPISODE_TIMEOUT_S` | 600 | 单条总时限(判定 + 比对) |
| `MAX_TOOL_CALLS` | 15 | 单条检索调用预算 |
| `SEARCH_TOP_K` / `SNIPPET_MAX_TOKENS` | 10 / 512 | 检索条数 / 摘要长度 |
| `RAG_API_BASE` / `RAG_TIMEOUT_S` | 空 / 30 | Zeus 地址 / 单次 HTTP 超时 |
| `ZEUS_APP_ID` / `ZEUS_APP_SECRET` / `ZEUS_INDEX` | 空 | Zeus 凭据与索引 |
| `ZEUS_SEARCH_LABELS` | 空(整个索引) | 检索标签过滤,逗号分隔 |
| `ZEUS_KB_LIMIT` / `ZEUS_MAX_K` / `ZEUS_RECALL_MIN_SCORE` / `ZEUS_RERANK_MIN_SCORE` / `ZEUS_MAX_CONCURRENCY` | 20 / 50 / 0.1 / 0 / 2 | Zeus 召回参数 |

## 6. 检索范围

外规与内规分库存放,本模块只检索内规库。Zeus 只使用 `standard_search`,不做解析、入库或列表。
