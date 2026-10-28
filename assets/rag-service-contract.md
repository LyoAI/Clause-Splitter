# RAG 服务接入说明

本服务(条款拆分与内外规比对)不包含检索引擎:解析、分块、索引、检索全部由**外部 RAG 服务**提供。
业务代码只依赖 `clause_splitter/rag/base.py` 的 `RagService` 抽象类;具体服务是 provider,
由 `RAG_PROVIDER` 选择。当前已实现 `chillrag`(本地开发)与 `zeus`(内网知识库)。

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

并非每个服务都暴露全部端点。`RagService` 用三个类属性声明能力(默认 `True`),
调用方一律 `getattr(service, name, True)` 判断(测试替身是鸭子类型):

| 属性 | 关闭后的行为 |
|---|---|
| `supports_parse` | 拆条不尝试远端解析,直接本地抽取 |
| `supports_document_listing` | 不调用 `documents()` / `stats()`;复核页内规列表/计数为空 |
| `supports_document_chunks` | agent 不提供 `get_context` 工具(无按文档列切片端点) |

内部类型(`clause_splitter/rag/types.py`):

- `Chunk(chunk_id, doc_id, order, text, meta)` — `chunk_id` 在一次运行内必须稳定,
  grounding 依赖它;`doc_id` 是制度名,`order` 是文档内阅读顺序。
- `Hit(chunk_id, text, score, doc_id, section)` — `text` 必须是切片全文(给模型截断,grounding 用原文)。
- `DocInfo(doc_id, name, chunk_count, state)` — `state`: `indexed` / `indexing` / `failed`。
- `CorpusStats(doc_count, chunk_count, version)` — `version` 变化代表语料变化,写进 `analysis_index_signature`。

错误语义(`clause_splitter/rag/errors.py`):

分析阶段开始前会先做一次检索预检(`compare.aggregate._probe_retrieval`)。单条
episode 内的 5xx 会降级成"需人工复核",但检索整体不可用时会在跑任何 episode 前
直接失败 —— 否则整篇会变成"全部缺失"的假报告。

| 情况 | 异常 | 业务处理 |
|---|---|---|
| 未配置地址 | `RagNotConfigured` | 分析失败(可读提示);拆条走本地回退 |
| 连接失败 / 超时 / 5xx / 409 | `RagUnavailable` | 拆条回退;单条候选为空,但分析阶段的检索预检会让整阶段失败 |
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

## 3. Zeus provider 映射

实现:`clause_splitter/rag/providers/zeus.py`。Zeus 把解析、分块、embedding、入库都封装在
文档提交后的后台流程里,对外只暴露「上传文件 → 提交文档 → 检索」;每个请求都带
`x-app-id` / `x-app-secret` / `x-index` / `x-request-id`(每请求新 uuid)。

| RagService | Zeus |
|---|---|
| `available` | `RAG_API_BASE` 与 `ZEUS_APP_ID` / `ZEUS_APP_SECRET` / `ZEUS_INDEX` 全部非空 |
| `ingest` | ① `POST /api/v1/uns/text_data_process/app/file`(multipart 字段 `file`)→ 文件 URL;② `POST /api/v1/uns/text_data_process/app/doc/{x-index}`,body 为**单个对象**(不是数组,发数组会 500 `readObjectStart: expect { or n, but found [`):`org_data_id` / `file_name` / `file_type` / `file_size` / `url` / `text_labels` / `created_by`。提交成功即受理,返回 `DocInfo(doc_id=org_data_id, chunk_count=0, state="indexed")` |
| `search` | `POST /api/v1/uns/zeus-search/{x-app-id}/server/standard_search`;`kb[0].alias = x-index`,`filter.data_type=["doc"]`,`kb[0].limit = max(k, ZEUS_KB_LIMIT)` 为召回条数,顶层 `limit = min(k, ZEUS_MAX_K)` 为最终条数;行映射 `id → chunk_id`、`text`(空则 `data`)`→ text`、`score`(空则 `recall_score`)`→ score` |
| `parse` | 无文本返回 → `supports_parse=False`,外规拆分走本地抽取 |
| `fetch_chunks` | 无按文档列切片端点 → `supports_document_chunks=False`,`get_context` 工具不提供 |
| `documents` / `stats` | 无列表/统计端点 → 返回 `[]` / 空 `CorpusStats`;`get_materials` 不提供,复核页内规列表与计数为空 |
| `delete` | `DELETE .../doc/batch/{x-index}`,body 为 `org_data_id` 数组;404 视为已删除 |

注意:

- **语料靠标签隔离**:`x-app-id` / `x-index` 圈定的是整个知识库,库内可能同时有内规和
  附件,只有 `text_labels` 能区分。实测**提交时不打标签的文档检索不到**,所以标签是必需项。
  - 入库(内规):`text_labels=["internal-regulation", "doc:<文件名 stem>"]`
  - 入库(附件):`text_labels=["attach-regulation", "doc:<文件名 stem>"]`
  - 检索:默认 `filter.text_labels=["internal-regulation", "attach-regulation"]`;
    `ZEUS_SEARCH_LABELS` 不写或写 `all` → `text_labels=[]`(不过滤,整个知识库);
    写 `x,y` 时只在这些标签下检索。
  - 多值 filter 是 OR 还是 AND 尚未确认,联调时需验证。
- **制度名来自标签**:检索结果不带文件名,`search` 从 `doc:` 标签还原 `doc_id`(回退
  进程内 `org_data_id → 文件名` 映射,再回退 `org_data_id`)。语料若由其他系统入库
  (无 `doc:` 标签),复核页与 ESG 回调中的内规制度名会退化为 `org_data_id`。
- **入库异步**:提交成功只代表受理,后台解析/分块/embedding 完成后才可检索;Zeus 没有
  状态接口,`state` / `chunk_count` 只是乐观值。刚入库就检索可能为空,需等 Zeus 处理完。
- **检索 404 = 能力未开通**:文档说明搜索端点 404 表示该应用未配置检索,provider 转成
  `RagNotConfigured`(任务失败并提示联系 RAG 团队),不当作“无结果”。
- **并发 2**:文档写明并发上限 2;provider 用信号量(`ZEUS_MAX_CONCURRENCY`,默认 2)
  限制同时在途的 HTTP 请求,排队超时按 `RagUnavailable` 处理。
- **重试语义**:重复 `org_data_id` 是覆盖还是报错文档未说明,联调时需确认
  `POST /library/index` 的重复入库行为。
- 配置(`clause_splitter/config.py`):

```
RAG_PROVIDER=zeus
RAG_API_BASE=http://<zeus 域名>
ZEUS_APP_ID=
ZEUS_APP_SECRET=
ZEUS_INDEX=
ZEUS_KB_LIMIT=20              # 每次检索的召回调大条数
ZEUS_MAX_K=50                 # 最终返回条数上限
ZEUS_RECALL_MIN_SCORE=0.1     # 召回过滤阈值
ZEUS_RERANK_MIN_SCORE=0.0     # 0 = 不发送顶层 min_score
ZEUS_MAX_CONCURRENCY=2        # 网关并发上限
ZEUS_UPLOAD_TIMEOUT_S=300     # 上传单次超时
ZEUS_CREATED_BY=<UM 号>       # 提交时的上传人
ZEUS_LABEL_INTERNAL=internal-regulation
ZEUS_LABEL_ATTACH=attach-regulation
ZEUS_SEARCH_LABELS=           # 空/all = text_labels=[](整个知识库);x,y = 指定标签
```

## 4. 接入新内网 provider

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
