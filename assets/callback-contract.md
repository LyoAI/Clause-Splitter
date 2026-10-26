# 回调 / 拉取出参规范

同一份报文用于两处:**ESG 网关回调**(`POST {esg_open_end_point}/paerm/paermModelReturn?access_token=…&request_id=…`)和 **`GET /api/v1/clause_splitter/regulations/{docId}/result`**。后端只写一套解析器。

结构沿用旧版:文档级字段全部在顶层,条款级字段放在 `data` 数组里;本次只在旧结构上新增了 `applicable` / `applicableReason` 与条款级分析字段。

## 1. 顶层

```json
{
  "code": "1",
  "msg": "成功",
  "docId": "D001",
  "docKeyword": "数据安全, 风险管理, 信息披露",
  "docType": "2",
  "docAbstract": "……",
  "taskId": "T001",
  "applicable": true,
  "applicableReason": "适用于平安科技",
  "data": []
}
```

字段顺序固定为:`code`、`msg`、`docId`、`docKeyword`、`docType`、`docAbstract`、`taskId`、`applicable`、`applicableReason`、`data`。

| 字段 | 说明 |
|---|---|
| `code` | `"1"` 链条执行完毕(含"不适用"的正常终止);`"0"` 技术失败;`"2"` 处理中(排队/执行中) |
| `msg` | `code=1` 时为 `"成功"` 或具体终止原因;`code=0` 为错误摘要;`code=2` 为 `"处理中"` |
| `docId` | 推送入参 `docId`,后端定位法规的唯一依据 |
| `docKeyword` | 文档级关键词,顿号/逗号分隔的字符串;由一次文档级 LLM 调用生成,失败时本地关键词兜底 |
| `docType` | 推送入参 `docType` |
| `docAbstract` | 文档级摘要(100–250 字);由同一次 LLM 调用生成,失败时正文截取兜底 |
| `taskId` | 推送入参 `taskId` |
| `applicable` | 是否适用于平安科技:`true` / `false` / `null`(需人工复核) |
| `applicableReason` | 适用性判定依据;无原因时为空串 |
| `data` | **条款数组,永远不为 `null`**;拆条未执行或无条款时为 `[]` |

`extraParams` 仍在 push 入参中接受并落库,但**不再出现在结果报文里**。

## 2. data 元素(每个条款一个 dict)

```json
{
  "chapter": "第一章 总则",
  "orderNum": "0",
  "relationTerms": [],
  "termContent": "……",
  "termId": "D001-0",
  "termKeyword": "数据安全, 风险管理",
  "termName": "第一条",
  "internalize": "需要内化",
  "internalizeReason": "……",
  "coverageStatus": "部分覆盖",
  "analysis": "……",
  "differenceAnalysis": "……",
  "coreRequirements": "1）、……",
  "remedySuggestion": "1）、……"
}
```

| 字段 | 说明 |
|---|---|
| `chapter` | 所属章节路径(末级为条款号时已剔除),无章节为空串 |
| `orderNum` | 条款顺序,**字符串**,从 `"0"` 开始 |
| `relationTerms` | grounding **采纳**的内规条款数组,无则为 `[]`;结构见下 |
| `termContent` | 条款正文 |
| `termId` | `{docId}-{orderNum}`,如 `D001-0` |
| `termKeyword` | 条款关键词,本地 jieba 提取 |
| `termName` | 条款号(第一条 / Article 1) |
| `internalize` | 内化判定,只有「需要内化 / 无需内化」两值 |
| `internalizeReason` | 内化判定依据 |
| `coverageStatus` | 覆盖状态:完全覆盖 / 部分覆盖 / 缺失;未执行分析时为空串 |
| `analysis` | 覆盖分析;未执行时为空串 |
| `differenceAnalysis` | 差异说明;无差异或未执行时为空串 |
| `coreRequirements` | 核心监管要求 |
| `remedySuggestion` | 整改建议 |

`relationTerms` 元素:

```json
{
  "internalClauseId": "数据安全管理办法#1",
  "docName": "数据安全管理办法",
  "clauseName": "第二条",
  "clauseContent": "……"
}
```

- 只包含 grounding 采纳的内规条款;批量预召回但未采纳的不下发。
- 外规条款没有关联内规时为 `[]`。
- 条款类型(`clause_type`:义务性/指导性/宣示性)仍在库内保留,本次不下发。

## 3. 分支示例

**不适用**

```json
{
  "code": "1",
  "msg": "法规不适用于本公司,未执行拆条与内化分析",
  "docId": "D001",
  "docKeyword": "银行, 资本管理",
  "docType": "2",
  "docAbstract": "……",
  "taskId": "T001",
  "applicable": false,
  "applicableReason": "规范对象为银行业金融机构,与平安科技业务无交集",
  "data": []
}
```

**处理中(pull 命中 scheduled/running)**

```json
{
  "code": "2",
  "msg": "处理中",
  "docId": "D001",
  "docKeyword": "",
  "docType": "2",
  "docAbstract": "",
  "taskId": "T001",
  "applicable": null,
  "applicableReason": "",
  "data": []
}
```

处理中不读取文档正文,`docAbstract` / `docKeyword` 可能为空。

**内化阶段失败(拆条结果仍然交付)**

```json
{
  "code": "0",
  "msg": "internalization failed: LLM timeout",
  "docId": "D001",
  "docKeyword": "数据安全, 风险管理",
  "docType": "2",
  "docAbstract": "……",
  "taskId": "T001",
  "applicable": true,
  "applicableReason": "……",
  "data": [
    {
      "chapter": "第一章 总则",
      "orderNum": "0",
      "relationTerms": [],
      "termContent": "……",
      "termId": "D001-0",
      "termKeyword": "数据安全",
      "termName": "第一条",
      "internalize": "",
      "internalizeReason": "",
      "coverageStatus": "",
      "analysis": "",
      "differenceAnalysis": "",
      "coreRequirements": "",
      "remedySuggestion": ""
    }
  ]
}
```

## 4. 查询接口与错误

- `GET /regulations/{docId}/result` 成功时**直接返回本规范的扁平报文,不再套统一信封**。
- 404 / 500 等错误仍使用统一错误信封(带非 2xx HTTP 状态):

```json
{
  "code": "404",
  "status": "error",
  "msg": "no pushed regulation for docId D001",
  "data": { "error_code": "HTTP_404", "details": {} }
}
```

后端应以 HTTP 状态码区分成功与失败,不要把错误信封当作结果报文解析。

## 5. 内部字段对照

| 回调字段 | 内部字段(DB / model) | 复核页(HTML)位置 |
|---|---|---|
| `docId` | `push_doc_id`(缺省时 regulation `_id`) | — |
| `docKeyword` | `doc_keyword` | 页面不展示 |
| `docType` | `push_doc_type` | — |
| `docAbstract` | `doc_abstract` | 页面不展示 |
| `taskId` | `push_task_id` | — |
| `applicable` | `applicability_applicable` | 适用性徽标 |
| `applicableReason` | `applicability_reason` | 适用性说明 |
| `orderNum` | `seq`(从 0 开始) | 排序 |
| `termId` | `docId` + `seq` | 卡片锚点 |
| `termName` | `clause_number` | 卡片头条款号 |
| `termKeyword` | `term_keyword` | 页面不展示 |
| `termContent` | `clause_text` | 外规原文(可编辑/批注) |
| `chapter` | `section` | 卡片头章节路径 |
| `internalize` | `internalization_decision` | 内化判定徽标 |
| `internalizeReason` | `internalization_reason` | ①栏「内化判定依据」 |
| `coverageStatus` | `status` | 覆盖状态徽标 |
| `analysis` | `analysis` | ②栏「判定分析」 |
| `differenceAnalysis` | `difference_analysis` | ②栏「差异说明」 |
| `coreRequirements` | `core_regulatory_requirements` | ①栏「核心监管要求」 |
| `remedySuggestion` | `remedy_suggestion` | ③栏「整改建议」 |
| `relationTerms` | `candidates[adopted]` / `matched_internal_*` | ④栏「采用内规」 |
| *(不下发)* | `clause_type`、`candidates` 全部召回、`push_extra_params` | 页面不展示 |
