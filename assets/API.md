# 外规内化服务 · 接口文档

版本 0.4.0 · 基础路径 `http://<host>:18998/api/v1` · 更新日期 2026-10-20

---

## 1. 通用约定

### 1.1 响应包裹

所有接口返回统一包裹:

```json
{
  "code": 0,          // 0=成功,非 0=失败
  "status": "SUCCESS",
  "data": { ... },    // 业务数据,失败时为 null
  "msg": ""           // 失败时的错误说明
}
```

错误响应的 `data` 中带机器可读 `error_code`(如 `HTTP_409`)。

### 1.2 鉴权

环境变量 `API_KEY` 非空时,所有 `/api/v1/*` 接口要求请求头 `X-API-Key: <key>`;未配置则不鉴权。错误返回 401。

### 1.3 处理流水线

外规走 4 个阶段,阶段状态机:`not_run → pending → running → done | failed | skipped`:

| 阶段 | 作用 | 前置条件 |
|---|---|---|
| applicability | 文档级适用性判定(是否适用于平安科技) | 无 |
| split | 拆条(第X条…) | applicability=done 且 applicable=true(`force` 可跳过) |
| internalization | 逐条内化判定(需要内化/无需内化) | split=done |
| analysis | 逐条差距分析/整改建议/报告(需要内化的条款) | internalization=done |

- 重跑某阶段会把它及其下游阶段重置为 `not_run` 并清空派生数据(不会出现新旧混杂)。
- 一个外规同时只允许一个任务在跑(否则 409)。
- 整链任务(run)内部按 0.10/0.15/0.25/0.50 权重汇报进度;判定不适用或需人工复核时下游标记 `skipped`。

### 1.4 常用错误码

| HTTP | 场景 |
|---|---|
| 400 | 参数缺失/非法 |
| 401 | X-API-Key 缺失或错误 |
| 404 | 资源不存在 |
| 409 | 阶段前置条件不满足 / 已有任务在跑 |
| 413 | 上传超过 `MAX_UPLOAD_MB` |
| 415 | 文件类型不支持 |
| 502 | 后端推送模式下文档下载失败 |

---

## 2. 外规管理 `/regulations`

### 2.1 上传外规

```
POST /regulations            Content-Type: multipart/form-data
  file: 必填,支持 pdf/docx/doc/txt/md
  title: 选填,默认取文件名
```

- 201 创建成功(200 = sha256 重复,返回已有记录,`data.duplicate=true`)
- 返回 `data`:外规对象(见 2.3)

```bash
curl -X POST http://localhost:18998/api/v1/regulations \
  -H "X-API-Key: $KEY" -F "file=@办法.pdf" -F "title=培训供应商管理细则"
```

### 2.2 外规列表

```
GET /regulations?limit=20&offset=0
→ data: { items: [外规对象...], total }
```

### 2.3 外规详情

```
GET /regulations/{id}
```

`data` 字段:

| 字段 | 说明 |
|---|---|
| id / title / source_filename | 基础信息 |
| article_count | 拆条后的条款数(未拆条为 null) |
| applicability | `{applicable: true\|false\|null(需人工复核), confidence, reason, truncated}` |
| stages | 四阶段状态,如 `{"applicability":"done","split":"running",...}` |
| split_mode | 拆条模式: mineru(确定性)/ llm(回退) |
| analysis_summary | 见 2.9 |
| error / created_at / updated_at | 其他 |

另附 `data.latest_job`(最近一次任务)。

### 2.4 删除外规

```
DELETE /regulations/{id}    → 204(有任务在跑时 409)
```

### 2.5 触发阶段 / 整链

```
POST /regulations/{id}/applicability     → 202 {job}
POST /regulations/{id}/split             → 202 {job}   body: {"force": true}(可选,不适用时强制拆条)
POST /regulations/{id}/internalization   → 202 {job}
POST /regulations/{id}/analysis          → 202 {job}
POST /regulations/{id}/run               → 202 {job}   # 整链 1→4
```

### 2.6 人工改判适用性

适用性判定失败开(fail-open)→ `applicable=null` 需人工复核,人工确认后用:

```
PATCH /regulations/{id}/applicability    body: {"applicable": true, "reason": "人工确认适用"}
```

### 2.7 条款列表 / 详情

```
GET /regulations/{id}/clauses?decision=需要内化&status=缺失&include=candidates&limit=50&offset=0
GET /regulations/{id}/clauses/{clause_id}
```

条款字段(详情额外带 `candidates` 检索召回):

| 字段 | 说明 |
|---|---|
| clause_number / section / clause_text | 条款号(第一条…)/所属章节/原文 |
| clause_type / internalization_decision / internalization_reason | 义务性/指导性/宣示性条款;需要内化/无需内化 |
| status | 完全覆盖 / 部分覆盖 / 缺失 / (无需内化时为空) |
| analysis / difference_analysis / core_regulatory_requirements / remedy_suggestion | 判定分析 / 差异说明 / 核心监管要求 / 整改建议 |
| matched_internal_id / _document_name / _clause_number / _source_text | 采用的内规条款(内容快照,不受索引重建影响) |
| matched_internal_clause_ids | 证据条款 id 列表 |
| candidates | 检索召回 top-k:`[{backend, chunk_id, doc_id, score, text, ...}]` |

### 2.8 任务查询

```
GET /regulations/{id}/jobs?limit=20
GET /jobs?state=running&job_type=chain
GET /jobs/{job_id}
```

`job` 字段:`id / job_type / regulation_id / state(queued|running|done|failed) / progress(0-1) / step / error / payload / created_at / updated_at`

### 2.9 产物下载

```
GET /regulations/{id}/artifacts                → [{name, url, size, created_at}]
GET /regulations/{id}/artifacts/{name}         → 文件流
```

| name | 内容 |
|---|---|
| structure.json | 拆条结构 |
| compare_result.json | 全量比对结果(含召回候选) |
| gap_report.md | 内外规合规差距分析报告 |
| review.html | 内规检索复核页(自包含,可发作业人员) |
| episodes/{clause_id}.json | 单条款 ReAct 轨迹(检索/推理过程) |

`analysis_summary`:

```json
{
  "coverage_rate": 0.5,
  "fully_covered_count": 1,
  "partially_covered_count": 1,
  "missing_count": 2,
  "internalizable_count": 4,
  "no_internalization_count": 1,
  "report_mode": "llm"
}
```

---

## 3. 内规库 `/library`

```
POST   /library/documents        multipart 上传(pdf/docx/pptx/图片/多制度 Excel)→ 201/200(重复)
GET    /library/documents        → {items, total}
DELETE /library/documents/{id}   → 204(构建索引期间 409)
POST   /library/index            body: {"rebuild": false} → 202 {job_id}
GET    /library/index            → {doc_count, chunk_count, ...}
```

- 上传只落盘不建索引;`POST /library/index` 触发增量构建(按 sha256 差异,增/改文档重嵌入,未变复用缓存)。
- 索引构建与分析任务互斥(读写锁):构建时分析等待,分析进行时构建等待,不会读到半新半旧的索引。

---

## 4. 后端对接 `/integration`(系统后端调用)

### 4.1 推送文档

```
POST /integration/push
```

入参(后端推送格式,原样透传):

```json
{
  "args": {
    "OptType": 0,
    "docContent": "",
    "docType": "3",
    "docId": "2ef71e627aee466bb351545033df3050",
    "doclobsUrl": "http://iobs.paic.com.cn/download/...?attname=平科办〔2017〕8号附件4：培训供应商管理细则（2017版）",
    "taskId": "170b82cd89de48c0a112b3ad0b69b659",
    "uid": "9760fde4-bf16-4caf-91d0-978360485df5"
  }
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| docId | 是 | 后端文档 id |
| taskId / uid | 否 | 任务与用户标识,回调时原样带回 |
| docContent | 否* | 有值时直接作为文档内容落库(按纯文本处理) |
| doclobsUrl | 否* | docContent 为空时必填;从链接下载文档,标题取 `attname` 参数 |
| docType | 否 | 文档类型(当前未参与逻辑) |
| OptType | 否 | 0=手动(视为一定适用于平安科技,**跳过阶段1判定**)/ 1=自动(走完整四阶段) |

\* docContent 与 doclobsUrl 至少一个,否则 400。

响应 202:

```json
{ "code": 0, "data": { "regulation_id": "a1b2c3", "job_id": "d4e5f6",
                       "task_id": "170b...", "manual": true, "duplicate": false } }
```

### 4.2 结果回调(草案,以后端最终契约为准)

配置 `BACKEND_CALLBACK_URL` 后,push 发起的整链任务到达终态(成功或失败)时,服务端 POST 回调:

```json
{
  "args": { "docId": "...", "taskId": "...", "uid": "..." },
  "status": "done",
  "error": "",
  "regulationId": "a1b2c3",
  "applicable": true,
  "summary": { "coverage_rate": 0.5, "...": "同 analysis_summary" },
  "clauses": [
    { "clauseNumber": "第一条", "text": "...", "decision": "需要内化",
      "coverage": "完全覆盖", "analysis": "...", "differenceAnalysis": "...",
      "remedySuggestion": "..." }
  ]
}
```

> ⚠️ 待与后端对齐的点(都收敛在 `clause_splitter/services/integration_service.py` 一个文件里,调整不影响流水线):
> 1. `OptType` 0/1 与手动/自动的对应关系(当前按 0=手动);
> 2. `docContent` 的语义(纯文本 / base64);
> 3. `docType` 与文件后缀的映射(当前从 attname 后缀推断,无后缀默认 `.pdf`);
> 4. 回调的最终字段契约与鉴权方式。

---

## 5. 健康检查

```
GET /healthz        → 200
```

---

## 6. 典型调用时序(后端视角)

```
后端                          本服务
 │ POST /integration/push ────▶│ (下载/落库,排队整链任务)
 │◀── 202 {job_id} ────────────│
 │                             │ 阶段1 适用性 → 阶段2 拆条 → 阶段3 内化判定 → 阶段4 分析+报告
 │ (可选) GET /jobs/{job_id} ─▶│ 轮询进度 progress/step
 │◀── {state:"done"} ──────────│
 │◀── POST 回调(最终结果)──────│ 配置 BACKEND_CALLBACK_URL 时
 │ (可选) GET .../artifacts ──▶│ 下载 gap_report.md / review.html / episodes
```

人工介入点:适用性需人工复核时(applicable=null),作业人员通过 `PATCH .../applicability` 改判后再触发后续阶段。
