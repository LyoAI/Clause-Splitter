# RAG 服务接口契约

本服务(条款拆分与内外规比对)不包含检索引擎:解析、分块、索引、检索全部由**外部 RAG 服务**提供。
本文档是该服务的 HTTP 契约,客户端实现见 `clause_splitter/rag/client.py`。

- 本地联调:把 `RAG_API_BASE` 指向自建的 chillrag HTTP 服务(如 `http://127.0.0.1:8000`)。
- 内网对接:拿到对方 URL 与真实出入参后,**只改 `clause_splitter/rag/client.py` 的请求/响应映射**
  (必要时在 `types.py` 加可选字段);业务代码、拆条、比对、出参一律不动。
- 客户端对字段名做了容错(见各端点"字段别名"),对方字段风格不同时优先靠别名兼容,仍不匹配再改映射。

## 1. 通用约定

| 项 | 约定 |
|---|---|
| Base URL | `RAG_API_BASE`,不带末尾 `/`;为空 = 未配置 |
| 鉴权 | 配置 `RAG_API_KEY` 时发送 `Authorization: Bearer <key>` |
| 编码 | 请求/响应 UTF-8;`Content-Type: application/json` 或 `multipart/form-data` |
| 超时 | 普通调用 `RAG_TIMEOUT_S`(默认 30s);上传解析/入库 `RAG_PARSE_TIMEOUT_S`(默认 300s) |
| 重试 | 仅 GET 对 502/503/504 自动重试 2 次;写请求不自动重放 |
| 错误 | 401/403 → 鉴权失败;404 → 资源不存在;其余 ≥400 → 契约/业务错误;超时/连接失败 → 不可用 |
| 幂等 | `POST /documents` 按 `doc_id` 覆盖式 upsert;`DELETE` 对不存在的文档返回 404 也可接受(客户端按"已删除"处理) |
| 并发 | 同时最多约 `COMPARE_CONCURRENCY`(默认 2)个 `/search`,可能叠加 1 个上传/入库;检索需支持并发读 |

响应体可以是裸数组,也可以是带包装键的对象(客户端依次尝试 `hits` / `data` / `items` / `results`);
对象端点同理接受 `chunks` → `data` → `items` → `results`、`documents` → `data` → `items`。

## 2. 端点一览

| 方法 | 路径 | 用途 | 调用方 |
|---|---|---|---|
| POST | `/parse` | 上传外规文档,返回解析后的全文 / markdown / blocks | 拆条(阶段 2) |
| POST | `/documents` | 内规入库(解析 + 分块 + 建索引) | 内规库上传 / 索引任务 |
| GET | `/documents` | 文档列表 | 报告头、`get_materials` 工具 |
| DELETE | `/documents/{doc_id}` | 删除文档及其切片 | 内规库删除 |
| GET | `/documents/{doc_id}/chunks` | 按阅读顺序返回全部切片 | `get_context` 工具、grounding |
| POST | `/search` | 混合检索 top-k | 比对 agent、复核页预召回 |
| GET | `/stats` | 文档数 / 切片数 / 索引版本 | 索引状态、分析快照签名 |

## 3. 端点明细

### 3.1 POST /parse — 解析文档

`multipart/form-data`:

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `file` | 文件 | 是 | pdf / docx / pptx / xlsx / txt / md 等 |
| `doc_id` | string | 是 | 调用方给出的文档标识(通常为文件名 stem) |

响应 `200`(二选一):

1. `application/json`:

```json
{
  "doc_id": "金融数据安全管理办法",
  "text": "第一章 总则\n第一条 ……",
  "markdown": "# 第一章 总则\n\n第一条 ……",
  "blocks": [
    {"type": "title", "text": "第一章 总则", "level": 1, "page": 1},
    {"type": "paragraph", "text": "第一条 ……", "page": 1}
  ],
  "meta": {}
}
```

2. `text/plain`:直接把全文放在响应体里。

字段说明:

- `text` / `markdown` 至少一个非空;两者都给时**优先 markdown**(标题层级提升拆条准确率)。
- `blocks` 可选;给了就优先使用(客户端保留 `type` / `level` / `page` / `order`)。
  只返回 `blocks` 也可接受(客户端不会再要求顶层全文)。
  识别类型:`title`(带 `level`,1 为最高)、`paragraph`、`table`;页眉页脚类可标 `header` / `footer` / `page_number`。
- 别名兼容:`text` ← `content` / `full_text`;`markdown` ← `md`;块内 `text` ← `content`。

**拆条依赖**:必须返回可重组的**全文**(或 headinglevel 完整的 markdown)。
只返回 top-k 切片无法拆条;若只提供切片,切片必须带 `chunk_index` 与原文 `offset` 且互不重叠,
否则请补充全文接口 —— 这是对接的第一优先级需求。

远端超时/非 2xx/空文本时的处理:`PARSE_FALLBACK=local`(默认)走本地纯文本抽取
(pdfplumber / python-docx / openpyxl / md / txt),`none` 则直接失败。实际路径记录在
`structure.json` 的 `parse_source`(`remote` / `local`)。

### 3.2 POST /documents — 内规入库

`multipart/form-data`:

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `file` | 文件 | 是 | 内规原文 |
| `name` | string | 是 | 展示名(制度名) |
| `doc_id` | string | 是 | 幂等键;重复提交即覆盖旧版本 |

响应 `200/201`:

```json
{"doc_id": "d4f3…", "name": "数据安全管理办法", "chunk_count": 42, "state": "indexed"}
```

- 别名:`doc_id` ← `id`;`name` ← `file_name` / `filename`;`state` ← `status`。
- `state` 建议:`indexed`(可检索)/ `indexing` / `failed`。同步返回时用 `indexed`;
  若为异步入库,先返回 `indexing`,并保证 `GET /documents` 能看到最终状态。
- 服务端负责解析与分块。**内规必须按"条"分块**:一个切片 = 一条(第X条),`chunk_id` 稳定且唯一,
  `section` 带章节路径(见 3.5)。

### 3.3 GET /documents — 文档列表

```json
{"documents": [
  {"doc_id": "d4f3…", "name": "数据安全管理办法", "chunk_count": 42, "state": "indexed"}
]}
```

### 3.4 DELETE /documents/{doc_id} — 删除文档

- `204` 或 `200` 表示成功;文档不存在返回 `404`(客户端视为已删除)。
- 删除后 `stats().version` 必须变化。

### 3.5 GET /documents/{doc_id}/chunks — 文档切片

```json
{"chunks": [
  {
    "chunk_id": "数据安全管理办法#0",
    "doc_id": "数据安全管理办法",
    "doc_name": "数据安全管理办法",
    "section": "第一章 总则 > 第一条",
    "text": "第一条　本制度适用于……",
    "order": 0,
    "meta": {"clause_number": "第一条", "page": 3}
  }
]}
```

要求:

- 按 `order` / `chunk_index` 升序返回全部切片。
- `chunk_id` 在语料内唯一;推荐 `{doc_id}#{序号}`(客户端 grounding 依赖它稳定)。
- `section` 为章节路径,用 `" > "` 连接;最后一级是条款号(如 `第二条`),
  复核页据此展示"制度名 第X条"。无标题文档可只给 `order`。
- 字段别名:`chunk_id` ← `id`;`text` ← `content` / `snippet`;`order` ← `chunk_index` / `index`;
  `doc_id` ← `doc` / `document_name`。

### 3.6 POST /search — 检索

请求:

```json
{"query": "金融机构应当建立数据分类分级制度", "top_k": 10}
```

响应:

```json
{"hits": [
  {
    "chunk_id": "数据安全管理办法#0",
    "doc_id": "数据安全管理办法",
    "doc_name": "数据安全管理办法",
    "section": "第一章 总则 > 第一条",
    "text": "第一条　本制度适用于……",
    "score": 0.83,
    "meta": {"clause_number": "第一条"}
  }
]}
```

- `text` 返回切片全文(客户端会自行截断给模型;grounding 需要原文)。
- `score` 越大越相关;可为 `null`,不可为字符串。
- 允许同一 `chunk_id` 多次返回,客户端取最高分并去重。
- 检索后端(向量/BM25/混合)由服务侧决定,对客户端不透明。

### 3.7 GET /stats — 语料统计

```json
{"doc_count": 12, "chunk_count": 486, "version": "2026-10-26T12:00:00Z-486"}
```

- `version`(`index_version` 亦可)在语料内容变化时**必须变化**;
  客户端将其写进 `analysis_index_signature`,用于判断分析结果是否对应同一版内规库。
- 无法提供版本时可给空串,但签名将为空。

## 4. 客户端错误映射

| 情况 | 异常 | 业务处理 |
|---|---|---|
| `RAG_API_BASE` 为空 | `RagNotConfigured` | 分析/链式任务失败,提示配置;拆条走本地回退 |
| 连接失败 / 超时 / 5xx | `RagUnavailable` | 拆条回退本地;检索候选为空、覆盖判定按缺失处理 |
| 401 / 403 | `RagAuthError` | 任务失败,提示检查 `RAG_API_KEY` |
| 404 | `RagNotFound` | 删除视为已完成;其他场景任务失败 |
| 其他 4xx / 响应结构不符 | `RagResponseError` | 任务失败,记录响应摘要 |

## 5. 对接确认清单

1. `/parse` 能否返回**全文或 markdown**?字段名与 `blocks` 是否可用?
2. `/documents` 入库是同步还是异步?如何轮询状态?
3. 内规分块粒度是否为"按条"?`chunk_id` / `section` 格式?
4. `/search` 的 `top_k` 上限、超时、单次返回条数?
5. 鉴权方式(固定 Bearer / 签名 / 网关 token)?
6. `version` 的语义与稳定性?
