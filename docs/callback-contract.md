# 回调 / 拉取出参规范

同一份报文用于两处:**ESG 网关回调**(`POST {esg_open_end_point}/paerm/paermModelReturn?access_token=…&request_id=…`)和 **`GET /api/v1/regulations/{docId}/result`**。后端只写一套解析器。

## 1. 顶层

```json
{
  "code": "1",
  "msg": "成功",
  "metadata": { "docId": "D001", "taskId": "T001", "uid": "U1", "docType": "2", "optType": 1 },
  "stages": { "applicability": "done", "split": "done", "internalization": "done", "analysis": "done" },
  "data": { "applicability": {}, "split": {}, "internalization": {}, "analysis": {} }
}
```

| 字段 | 说明 |
|---|---|
| `code` | `"1"` 链条执行完毕(含"不适用"的正常终止);`"0"` 技术失败;`"2"` 处理中(排队/执行中) |
| `msg` | `code=1` 时为 `"成功"` 或具体终止原因;`code=0` 为错误摘要;`code=2` 为 `"处理中"` |
| `metadata` | 推送入参回显,后端定位法规的唯一依据:`docId` / `taskId` / `uid` / `docType` / `optType`(`doclobsUrl` 不回显——可能含下载令牌) |
| `stages` | 四阶段状态,取值只有 `done` / `failed` / `pending` |
| `data` | 按阶段组织的结果;**某阶段无结果时该字段为 `null`**;四个阶段都无结果时 `data` 本身为 `null` |

### 阶段状态三值

| 状态 | 含义 |
|---|---|
| `done` | 已了结:执行完成,或被上游分支判定无需执行(如不适用的后三阶段) |
| `failed` | 执行出错 |
| `pending` | 尚未了结:排队 / 执行中 / 中断待下次重跑 |

- 手动推送(`optType=0`)跳过适用性 LLM 判定,`applicability` 仍为 `done`,结果为 `applicable=true`、`reason="手动推送(OptType=手动),默认适用于平安科技"`
- 不适用时后三阶段为 `done` + `data` 对应字段 `null`(永不执行,不是还在等待)

## 2. data 按阶段

### applicability

| 字段 | 说明 |
|---|---|
| `applicable` | `true` / `false` / `null`(需人工复核) |
| `reason` | 判定依据 |

适用性把握度(`applicability_confidence`)只保留在内部运维接口,不进回调报文。

### split(拆条)

```json
{
  "clauseCount": 2,
  "clauses": [
    { "clauseId": "D001_1", "orderNum": 1, "chapter": "第一章 总则",
      "clauseName": "第一条", "clauseContent": "…" }
  ]
}
```

| 字段 | 说明 |
|---|---|
| `clauseId` | `docId_序号`,跨文档唯一,回调内部关联键 |
| `orderNum` | 条款顺序,从 1 开始 |
| `chapter` | 所属章节路径(末级为条款号时已剔除),无章节为空串 |
| `clauseName` | 条款号(第一条 / Article 1) |
| `clauseContent` | 条款正文 |

### internalization(内化判定)

```json
{ "clauses": [
  { "clauseId": "D001_1", "clauseType": "义务性条款",
    "internalize": "需要内化", "internalizeReason": "…" }
] }
```

`internalize` 只有「需要内化 / 无需内化」两值。

### analysis(差距分析)

```json
{
  "coverageRate": "12/32",
  "fullyCoveredCount": 4, "partiallyCoveredCount": 8, "missingCount": 20,
  "internalizableCount": 32, "noInternalizationCount": 3,
  "clauses": [
    { "clauseId": "D001_1", "coverageStatus": "部分覆盖",
      "analysis": "…", "differenceAnalysis": "…",
      "coreRequirements": "1）、…", "remedySuggestion": "1）、…",
      "relationClauses": [
        { "internalClauseId": "数据安全管理办法#1", "docName": "数据安全管理办法",
          "clauseName": "第二条", "clauseContent": "…" }
      ] }
  ]
}
```

- `coverageRate` = 已覆盖(完全+部分)/ 需内化条款数,与复核页展示一致;无需内化条款不进分母
- `clauses` 只包含真正执行了差异分析的条款(「无需内化」条款不出现,其判定见 `internalization`)
- `relationClauses` 只包含 grounding **采纳**的内规条款;批量预召回但未采纳的不下发
- 页面按"部分覆盖"才展示 `differenceAnalysis` 是渲染策略;报文中该字段原样下发

## 3. 分支示例

**不适用**
```json
{
  "code": "1",
  "msg": "法规不适用于本公司,未执行拆条与内化分析",
  "metadata": { "docId": "D001", "taskId": "T001", "uid": "U1", "docType": "2", "optType": 1 },
  "stages": { "applicability": "done", "split": "done", "internalization": "done", "analysis": "done" },
  "data": {
    "applicability": { "applicable": false, "reason": "规范对象为银行业金融机构,与平安科技业务无交集" },
    "split": null, "internalization": null, "analysis": null
  }
}
```

**处理中(pull 命中 scheduled/running)**
```json
{
  "code": "2", "msg": "处理中",
  "metadata": { "...": "..." },
  "stages": { "applicability": "done", "split": "pending", "internalization": "pending", "analysis": "pending" },
  "data": { "applicability": {}, "split": null, "internalization": null, "analysis": null }
}
```

**内化阶段失败(拆条结果仍然交付)**
```json
{
  "code": "0", "msg": "internalization failed: LLM timeout",
  "stages": { "applicability": "done", "split": "done", "internalization": "failed", "analysis": "done" },
  "data": { "applicability": {}, "split": { "clauseCount": 2, "clauses": [] }, "internalization": null, "analysis": null }
}
```

## 4. 内部字段对照

| 回调字段 | 内部字段(DB / model) | 复核页(HTML)位置 |
|---|---|---|
| `clauseId` | 由 `docId` + `seq` 生成 | 卡片锚点 `clause_id`(页面用条款号) |
| `orderNum` | `seq + 1` | 排序 |
| `chapter` | `section` | 卡片头章节路径 |
| `clauseName` | `clause_number` | 卡片头条款号 |
| `clauseContent` | `clause_text` | 外规原文(可编辑/批注) |
| `clauseType` | `clause_type` | 页面不展示 |
| `internalize` | `internalization_decision` | 内化判定徽标 |
| `internalizeReason` | `internalization_reason` | ①栏「内化判定依据」 |
| `coverageStatus` | `status` | 覆盖状态徽标 |
| `analysis` | `analysis` | ②栏「判定分析」 |
| `differenceAnalysis` | `difference_analysis` | ②栏「差异说明」 |
| `coreRequirements` | `core_regulatory_requirements` | ①栏「核心监管要求」 |
| `remedySuggestion` | `remedy_suggestion` | ③栏「整改建议」 |
| `relationClauses` | `candidates[adopted]` / `matched_internal_*` | ④栏「采用内规」 |
| *(不下发)* | `candidates` 全部召回 | ④栏「检索召回」 |
