# 单条外规内化分析 · 接口文档

模块:`clause_analysis`(算法侧,只处理**一条已拆好的条款**)。工程侧负责遍历条款、调用本模块、补齐文档级字段并回调 ESG。

## 1. 调用方式(进程内 Python)

```python
from clause_analysis import ClauseAnalyzer, ClauseInput

analyzer = ClauseAnalyzer.from_config()          # 启动时构建一次,可复用
for clause in clauses:                           # 工程的 for 循环
    res = await analyzer.analyze(
        ClauseInput(
            term_name="第十二条",
            term_content="网络运营者应当……",
            term_id="",                               # 条款 ID,原样透传到出参 termId
            document_name="网络数据安全风险评估办法",  # 可选
            section="第三章 风险评估内容",             # 无章节传 ""
        ),
        need_internalization=True,   # 外规(docType=1);内规/附件(2/3)传 False
        keyword_mode="rules",        # 关键词提取:rules(默认,正则) | llm(失败自动回退 rules)
    )
    element = res.to_esg()                       # data[] 元素中的算法侧字段
```

- `analyze` 是唯一对外方法;无状态,每次调用独立。可选参数 `search_labels`(list[str]):本次检索的标签范围,由调用方按条款主动传;**不传或为空 = 不加过滤,检索整个索引**。检索范围不在初始化时写死。
- `need_internalization`:是否执行内化分析。**外规(docType=1)传 `True`(默认)**;**内规/附件(docType=2/3)传 `False`**——只提取关键词,不做内化判定、不检索 Zeus(该路径不要求配置 Zeus),分析字段全空。
- `keyword_mode`:关键词提取方式,**内外规条款都要提取**,且同时作用于出参外层 `termKeyword` 与 `relationTerms[].termKeyword`。`rules`(默认)为毫秒级正则;`llm` 质量更好但每条多一次 LLM 调用(关联条款关键词由比对阶段顺带提取,不另起调用),任何失败自动回退 `rules`。按耗时预算逐次选择。
- 并发由调用方控制。内网 LLM 只有 2 个并发槽,并行度请 ≤ 2。
- 单条冒烟(真实 LLM + Zeus):`uv run python scripts/analyze_clause.py examples/clause_input.json`,加 `--raw` 输出完整结果、`--keyword-mode llm` 换关键词提取方式、`--skip-internalization` 看内规/附件路径。

## 2. 输入 `ClauseInput`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `term_content` | str | 是 | 条款原文,非空 |
| `term_name` | str | 是 | 条款名称(条款号),如 `第一条` / `Article 1` |
| `term_id` | str | 否 | 条款 ID,原样透传到出参 `termId`;取不到传 `""` |
| `document_name` | str | 否 | 外规名称(后端 docId 对应的法规名) |
| `section` | str | 否 | 所属章节路径,无则 `""` |

必填只有 `term_content` 与 `term_name`,与出参的 `termContent` / `termName` 一一对应;输入只需条款原文与元信息,不接受五元组等预拆分字段。

## 3. 输出:`to_esg()`

返回 ESG 回调 `data[]` 的一个元素中**算法侧**的字段:

```json
{
  "termId": "条款 ID(入参 term_id 原样回传)",
  "termContent": "条款原文(入参 term_content 原样回传)",
  "termKeyword": "风险评估、报送义务、整改措施",
  "termName": "第十二条",
  "internalize": "1",
  "internalizeReason": "……",
  "requireType": "1",
  "coverageStatus": "1",
  "analysis": "……",
  "differenceAnalysis": "……",
  "coreRequirements": "1）……",
  "remedySuggestion": "1）……",
  "relationTerms": [
    {
      "aiRelationBasis": "原文关键句",
      "relationDocId": "内规文档ID",
      "termContent": "关联条款原文",
      "termId": "关联条款ID",
      "termKeyword": "",
      "termName": "第二条"
    }
  ]
}
```

### 3.1 字段说明

**条款身份字段**(描述传入的这条**外规条款**本身;注意与 `relationTerms` 内的同名字段区分——那里的字段描述的是**关联的内规条款**):

| 字段 | 类型 | 说明 |
|---|---|---|
| `termId` | str | 外规条款 ID,入参 `term_id` 原样回传 |
| `termContent` | str | 外规条款原文,入参 `term_content` 原样回传 |
| `termKeyword` | str | 外规条款核心合规关键词,由**前置关键词提取阶段**产出(`keyword_mode` 选 `rules` / `llm`,llm 失败自动回退 rules);两种方式均无命中时为 `""` |
| `termName` | str | 外规条款名称(条款号),入参 `term_name` 原样回传 |

**分析结果字段**:

| 字段 | 类型 | 说明 |
|---|---|---|
| `internalize` | str | 内化判定,枚举见 3.2 |
| `internalizeReason` | str | 内化判定依据 |
| `requireType` | str | 要求类型,枚举见 3.2;判定无有效值时为 `""` |
| `coverageStatus` | str | 覆盖状态,枚举见 3.2;**无需内化时为 `""`**(未执行分析) |
| `analysis` | str | 覆盖分析;无需内化时为 `""` |
| `differenceAnalysis` | str | 差异说明(仅部分覆盖) |
| `coreRequirements` | str | 核心监管要求 |
| `remedySuggestion` | str | 整改建议 |
| `relationTerms` | array | 采纳的内规条款,无则 `[]` |

`relationTerms` 元素:

| 字段 | 说明 |
|---|---|
| `aiRelationBasis` | AI 关联依据:模型引用的支撑原文关键句;取不到时为 `""` |
| `relationDocId` | 关联内规文档 ID,即后端 `docId`(内规入库时作为 Zeus `org_data_id` 写入);取不到时为 `""` |
| `termContent` | 关联条款原文 |
| `termId` | 关联条款 ID(检索返回的 chunk id) |
| `termKeyword` | 关联条款关键词:默认 `rules` 正则从关联条款原文提取;`keyword_mode="llm"` 时由比对 LLM 在 answer 中顺带给出(缺失回退 rules);取不到为 `""` |
| `termName` | 关联条款名称(条款号) |

关联条款的 `relationDocId` / `termContent` / `termName` 仅在本次检索缓存中能查到时回填;查不到时 `termId` 仍保留,其余为 `""`。覆盖结论不因引用 id 无法回查而改变。

### 3.2 枚举取值

全部枚举值以**数字字符串**输出。

| 字段 | 取值 | 含义 |
|---|---|---|
| `internalize` | `"0"` | 无需内化(宣示性/程序性条款,如立法目的、适用范围、定义、附则) |
| | `"1"` | 需要内化(义务性或指导性条款;含预防性内化) |
| `requireType` | `"1"` | 义务性要求(以"应当/须"等设定积极义务) |
| | `"2"` | 禁止性要求(以"不得/禁止/严禁"等设定不作为要求) |
| | `"3"` | 鼓励性要求(以"鼓励/建议/支持"等倡导,或未设定具体义务) |
| | `""` | 判定未给出有效值 |
| `coverageStatus` | `"0"` | 缺失(未覆盖);检索异常等无法完成判定的情况也以此输出,并在 `analysis` 注明"需人工复核" |
| | `"1"` | 部分覆盖 |
| | `"2"` | 全部覆盖(完全覆盖) |
| | `""` | 未执行分析(无需内化) |

注:示例文件中的 `applicable`(0 不适用 / 1 适用 / 2 直接适用 / 3 间接适用)是**文档级**字段,由工程侧的适用性判断产出,不在本模块输出中。

## 4. 错误语义

| 情况 | 行为 |
|---|---|
| 关键词提取 `llm` 模式失败/超时/输出无法解析 | 自动回退 `rules` 正则(外层条款与 `relationTerms` 关联条款同规则);均无命中时 `termKeyword=""`,不抛出 |
| 内化判定 LLM 超时/失败/输出无法解析 | 按需要内化(`"1"`)处理,`internalizeReason` 注明原因,继续比对 |
| 单条比对超时(`EPISODE_TIMEOUT_S`)或执行异常 | `coverageStatus="0"`,`analysis` 注明"需人工复核",不抛出 |
| Zeus 检索异常(不可达 / 5xx) | 失败的检索不中断比对;若最终判定为缺失且出现过检索失败,降级为 `"0"` + 需人工复核,不抛出 |
| Zeus 未配置 | 抛出 `RagNotConfigured` |
| Zeus 鉴权失败 | 抛出 `RagAuthError` |

抛出的两类是部署问题,调用方应中止整批。注意:`"0"` 同时代表真实缺失和降级的需人工复核,两者只能靠 `analysis` 文案区分。

## 5. 配置(环境变量,`clause_analysis/config.py`)

| 变量 | 默认 | 说明 |
|---|---|---|
| `RUN_ENV` | 空(本地开发) | 部署环境路由:`stg` / `prd` 等。设置后 env 文件取 **`.stg.env` / `.prd.env`**(缺失则回退 `.env`),模型配置取 **`config/llm/<model>-<RUN_ENV>.yaml`**;不设置 = `.env` + `<model>.yaml`。`RUN_ENV` 须由部署平台/shell 注入,不读自 env 文件 |
| `LLM_PROVIDER` / `LLM_MODEL_NAME` | `openai` / `qwen3.6-27b` | 对应 `config/llm/<model>.yaml` 的 section(文件名见 `RUN_ENV`) |
| `LLM_TIMEOUT_S` | 180 | 单次 LLM 请求超时 |
| `EPISODE_TIMEOUT_S` | 600 | 单条总时限(判定 + 比对) |
| `MAX_TOOL_CALLS` | 15 | 单条检索调用预算 |
| `SEARCH_TOP_K` / `SNIPPET_MAX_TOKENS` | 10 / 512 | 检索条数 / 摘要长度 |
| `RAG_API_BASE` / `RAG_TIMEOUT_S` | 空 / 30 | Zeus 地址 / 单次 HTTP 超时 |
| `ZEUS_APP_ID` / `ZEUS_APP_SECRET` / `ZEUS_INDEX` | 空 | Zeus 凭据与索引 |
| `ZEUS_KB_LIMIT` / `ZEUS_MAX_K` / `ZEUS_RECALL_MIN_SCORE` / `ZEUS_RERANK_MIN_SCORE` / `ZEUS_MAX_CONCURRENCY` | 20 / 50 / 0.1 / 0 / 2 | Zeus 召回参数 |

## 6. 检索范围

Zeus 用 `text_labels` 区分同一索引内的不同库(如内规库、外规库)。检索范围只由调用方决定,**不在客户端初始化时写死**:

- `analyze(clause, search_labels=["x"])` —— 本次分析的所有检索限定在这些标签下;
- `analyze(clause)` —— 不加标签过滤,检索整个索引。

标签选择完全由代码控制,不暴露给模型(避免模型编造标签造成假性 0 命中被当成"缺失")。

命中条目的文档 ID(`relationDocId`、工具返回 `meta.org_data_id`)取自 Zeus 行的 `org_data_id`,即内规入库时写入的文档 ID(后端 docId);标签文本不做任何解析假设。
