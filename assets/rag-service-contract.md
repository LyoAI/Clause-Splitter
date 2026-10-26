# RAG 服务接入说明

本服务(条款拆分与内外规比对)不包含检索引擎:解析、分块、索引、检索全部由**外部 RAG 服务**提供。
业务代码只依赖 `clause_splitter/rag/base.py` 的 `RagService` 抽象类;具体服务是 provider,
由 `RAG_PROVIDER` 选择。当前已实现 `chillrag`,内网服务接入时新增一个 provider 即可。

```
业务代码(pipeline / compare / agent / library)
        │  只 import RagService 与内部类型
        ▼
clause_splitter/rag/base.py          ← 稳定契约(本文第 1 节)
        │
        ▼
clause_splitter/rag/providers/<x>.py ← wire 细节(登录、字段、端点映射)
        │
        ▼
外部 RAG HTTP 服务
```

## 1. Provider 接口(稳定契约)

`RagService` 定义项目需要的能力,不绑定任何远端协议:

| 方法 | 语义 |
|---|---|
| `available` (property) | 是否已配置到可以尝试调用;空地址 = False |
| `parse(path, doc_id) -> ParsedDoc` | 外规解析为纯文本(`ParsedDoc.doc_id/text/meta`) |
| `ingest(path, name, doc_id="") -> DocInfo` | 内规入库(解析 + 分块 + 建索引) |
| `search(query, k) -> list[Hit]` | 内规混合检索 top-k |
| `fetch_chunks(doc_id) -> list[Chunk]` | 某份内规的全部切片(按阅读顺序) |
| `documents() -> list[DocInfo]` | 内规语料文档列表 |
| `stats() -> CorpusStats` | 文档数 / 切片数 / 索引版本 |
| `delete(doc_id) -> bool` | 删除文档;不支持删除的 provider 返回 `False`(基类默认) |

内部类型(`clause_splitter/rag/types.py`):

- `Chunk(chunk_id, doc_id, order, text, meta)` — `chunk_id` 在一次运行内必须稳定,
  grounding 依赖它;`doc_id` 是制度名,`order` 是文档内阅读顺序。
- `Hit(chunk_id, text, score, doc_id, section)` — `text` 必须是切片全文(给模型截断,grounding 用原文)。
- `DocInfo(doc_id, name, chunk_count, state)` — `state`: `indexed` / `indexing` / `failed`。
- `CorpusStats(doc_count, chunk_count, version)` — `version` 变化代表语料变化,写进 `analysis_index_signature`。

错误语义(`clause_splitter/rag/errors.py`):

| 情况 | 异常 | 业务处理 |
|---|---|---|
| 未配置地址 | `RagNotConfigured` | 分析失败(可读提示);拆条走本地回退 |
| 连接失败 / 超时 / 5xx / 409 | `RagUnavailable` | 拆条回退;检索候选为空 |
| 鉴权失败 | `RagAuthError` | 任务失败,提示检查凭据 |
| 资源不存在 | `RagNotFound` | 删除视为已完成;其他场景失败 |
| 其他 4xx / 响应不符 | `RagResponseError` | 任务失败,记录响应摘要 |

配置(`clause_splitter/config.py`,均可用环境变量覆盖):

```
RAG_PROVIDER=chillrag             # 选择 provider
RAG_API_BASE=http://127.0.0.1:9022
RAG_API_USER=                     # provider 需要登录时使用
RAG_API_PASSWORD=
RAG_CORPUS=clause_splitter_internal
RAG_PARSE_CORPUS=clause_splitter_parse
RAG_TIMEOUT_S=30                  # 单次 HTTP
RAG_PARSE_TIMEOUT_S=1800          # 上传 + 入库轮询总时限
```

## 2. ChillRAG provider 映射

实现:`clause_splitter/rag/providers/chillrag.py`。所有端点、字段、cookie 都封在该文件内。

| RagService | ChillRAG |
|---|---|
| 登录 | `POST /space/api/passport/login` `{username, passwd}` → `space-auth` cookie;首次调用前登录,401 时重登一次 |
| 源(语料) | `GET /space/api/service/asset-file/resource` 找 `resourceName`;不存在时 `PUT .../resource`(admin)创建,无权限则由首次上传 `POST /api/upload?folder=` 创建 |
| `ingest` | `POST .../asset-file/web-upload?sourceId=N`(字段名 `files`)→ file id;轮询 `GET /api/upload/status?sourceId=N` 到 `state != processing`;`finish` 但目标文件 chunks=0 时调一次 `POST /api/upload/ensure`;返回 `DocInfo(doc_id=文件名 stem, chunk_count, state)` |
| `parse` | 先按支持后缀预检(`.pdf/.docx/.pptx/.xlsx/.md/.markdown/图片`;`.txt` 直接失败 → 本地回退);上传到 `RAG_PARSE_CORPUS`(文件名 = `sha1(绝对路径)[:16] + 后缀`,防重复);轮询完成后 `GET .../asset-file/segments/{file_id}`,`segment.content` 是 JSON 字符串 `{"content": "..."}`,按 `index` 排序后用 `\n\n` 拼成纯文本 |
| `search` | `POST /space/api/service/llmapi/ht/normal_rag_recall_without_doc_group`,参数全在 query string:`question`、`source_ids=<内部语料 id>`、`origin_ids=[]`、`context_trace_ids=`;`source → chunk_id`、`segment → text`、`metadata("文件名称:X") → doc_id`;同一 chunk_id 取最高分去重后截断到 k;`section` 取首行(通常 `第X条`) |
| `fetch_chunks` | 通过 `upload/status` 的 `name → fileId` 找到文件,再取 `segments`,拼 `Chunk(chunk_id="{fileId}#{index}")` |
| `documents` / `stats` | 只统计 `RAG_CORPUS`:`GET /api/upload/status`;`version = "chillrag-v1-" + sha1(排序后的 name:fileId:chunks)[:16]`,空语料为空串 |
| `delete` | ChillRAG 无删除端点 → 不覆盖基类,返回 `False`;本地删除照常,远端旧文档由 RAG 侧清理 |

注意:

- ChillRAG 的文档标识是**文件名**:内规使用上传文件名(stem 即 `doc_id`),重名会覆盖。
- 内规必须按“条”分块:服务端 `CHUNK_LEVEL=4`(编=1 章=2 节=3 条=4),一个切片 ≈ 一条。
  实测本项目的内规 docx 样本经 MinerU 转换后所有段落都是 `paragraph`(无标题层级),`CHUNK_LEVEL` 不会生效,
  只会按 `CHUNK_SIZE` 切块;需要条级切片时优先提供 PDF,或调小 `CHUNK_SIZE` 作为兜底。
- 响应统一信封 `{code, msg, data}`:先看 HTTP 状态,再验 `code == 0`。
- 登录功能只是 ChillRAG 的实现细节;新 provider 可按内网要求使用 Bearer、网关 token 或不鉴权。

## 3. 接入新内网 provider

1. 在 `clause_splitter/rag/providers/` 新增文件,继承 `RagService`,实现 7 个抽象方法;
   `delete` 仅在内网支持删除时覆盖。
2. 只在该文件处理鉴权、字段别名、分页、轮询等 wire 细节;业务代码不得 import provider。
3. 在 `clause_splitter/rag/__init__.py` 的 `_PROVIDERS` 注册一行(可带自己的配置项)。
4. `.env` 改 `RAG_PROVIDER=<name>` 与地址/凭据即可切换;`chillrag.py` 可直接删除。
5. 对接前确认:
   - 能否返回**文档全文或按阅读顺序的全部切片**(只有 top-k 无法拆条);
   - 内规分块粒度是否为“按条”,`chunk_id` 是否稳定;
   - 检索 top-k 上限与超时;
   - 鉴权方式与凭据刷新;
   - 语料 `version` 的语义(用于分析结果签名)。
