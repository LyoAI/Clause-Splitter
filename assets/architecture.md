# Clause_Splitter 架构文档

条款拆分与外规内化分析服务:接收系统后端推送的外部法规,拆成条级条款,逐条与内规库比对判定内化覆盖,结果经 ESG 网关回调返回。检索与文档解析由**外部 RAG 服务**(HTTP)提供,算法层(ReAct agent、拆条、grounding)与业务编排全部在本仓 `clause_splitter` 侧。

## 1. 总体架构

```
系统后端(平安 ESG)
   │ ① POST /api/v1/clause_splitter/regulations/push          (docId/doclobsUrl/docContent/OptType/extraParams…)
   ▼
FastAPI(api/)──► IntegrationService ──► 取内容(doclobsUrl 优先,docContent 兜底)
   │                    │                    魔数嗅探定后缀(URL 名不可信)
   │                    ▼
   │              Regulation 入库(sha256 去重)+ Chain Job 入队
   │                    │
   │         CHAIN_DISPATCH = auto │ cron │ immediate
   │           auto: OptType=0(手动)→ 立即;其余 → scheduled 等 cron
   │           cron: 全部 scheduled,CHAIN_CRON 定时放行(默认 0 20 * * mon-sat)
   │           immediate: 全部立即
   │                    │
   ▼                    ▼
JobRunner(clause_splitter/jobs.py, workers=1)
   │  每 job: 适用性 → 拆条 → 内化判定 → 差异分析(run_chain)
   ▼
完成后:ESG 网关回调(token 流,13012 刷新/13016 退避重试)
        或后端主动拉 GET /api/v1/clause_splitter/regulations/{docId}/result
```

- **对外只有两个接口**(backend 面):`POST /regulations/push` 收数、`GET /regulations/{docId}/result` 拉结果;结果主动推送走 ESG 固定 callback。`/regulations/*` 其余路由、`/library`、`/jobs` 都是内部运维接口。
- 法规 = 拆分前的文档(后端 docId 对应),条款 = 拆分后的 第X条/Article 级单元。
- 单服务部署:算法与编排同进程,uvicorn 单 worker;重启恢复会把孤儿 queued/running 任务标 failed,scheduled 任务存活等下次 cron。
- 外部依赖只有一个 **RAG 服务**:负责外规解析、内规入库/分块/索引、检索。业务代码只依赖 `RagService` 抽象(`clause_splitter/rag/base.py`),具体实现由 `RAG_PROVIDER` 选择(当前 `chillrag`,地址 `RAG_API_BASE`);契约见 [rag-service-contract.md](rag-service-contract.md)。未配置时:`/healthz` 正常、拆条走本地文本抽取、分析类任务以可读错误失败。

## 2. LLM 双通道与并发预算

| 通道 | Provider | 模型 | 用途 |
|---|---|---|---|
| 内网 | `pingan`(平安网关) | qwen3.8-27b,仅 2 并发槽 | 生产 |
| 外网 | `openai` | deepseek-v4-flash | 开发/冒烟 |

并发按 2 槽设计:episode 扇出 `COMPARE_CONCURRENCY=2`、chain 串行 `JOB_WORKERS=1`,最坏在飞 LLM 请求 = 2。模型配置在 `config/llm/<model_name>.yaml`(gitignore,example 入库),默认模型 `Settings.default_model = qwen3.8-27b`。

## 3. 拆条:四层阶梯(scheme ladder)

```
取 blocks:parsing.router.parse_document
           provider.parse 远端解析(纯文本)优先
           失败/未配置 → 本地纯文本抽取(pdfplumber/docx/openpyxl/md/txt,无 OCR)
           → blocks(markdown 的 # 映射标题层级;纯文本按行)
① LLM 方案推断(1 次结构化调用,prompt split_scheme.md)
    输入 block 清单(前缀压缩),输出 {"patterns":[{pattern,level,kind}]}
② 代码校验+应用(确定性,LLM 不切文本)
    校验:正则可编译/level 1-4 不重复/≥1 article 且最深层/article 命中 ≥3
    应用:泛化 headings.py —— article 标记→标题+正文两块,section→整行标题
    → chunk_doc(level=CHUNK_LEVEL=4) → 体检(≥2 条且无超长畸形块)
③ 内置正则方案 compare/headings.infer_regulation_headings(编章节条+顿号,零 LLM)
④ analyze_structure 全文 LLM/正则切段(无编号文档兜底)
```

- 每层输出必须过体检才胜出,LLM 永不裸奔;①失败(挂/垃圾输出/校验不过)自然落到 ③。
- `structure.json` 记录 `split_layer ∈ {llm_scheme, builtin, llm_segment}`、生效的 `scheme` 与 `parse_source ∈ {remote, local}`;`split_mode` 同名返回。
- ① 层真实冒烟:英文 `Article N` 文档推出 `Article \d+` 方案;标准中文法规与 ③ 层输出等价。

## 4. 比对链(chain 四阶段)

| 阶段 | 行为 |
|---|---|
| applicability | LLM 判定法规是否适用于平安科技(手动推送 OptType=0 跳过,视为必适用) |
| split | 四层阶梯拆条,条款入库 |
| internalization | 逐条 LLM 判定条款类型/是否需要内化 |
| analysis | 逐条 ReAct episode:经 RAG 服务检索内规库 → grounding 证据 → 覆盖判定(完全/部分/缺失)+ 差异 + 整改建议 |

episode 语义:直接读取条款原文,先在内部逐项列出义务再逐项判断承接情况;不改变条级拆分粒度。

## 5. 后端对接细节(对齐旧版)

- **doclobsUrl 优先**,docContent(str 入参)兜底;URL 下载失败且有 docContent → 降级落盘,无兜底 → 502。
- **魔数嗅探**:`%PDF-`→.pdf、`PK\x03\x04`→.docx、`D0CF11E0`→.doc、可 UTF-8 解码→.txt、其余 415;文件名 = 标题(attname 或 docId)+ 嗅探后缀。
- docContent 是 str 不是文件:`_materialize_text` 显式适配(`<br/>`→换行)写内部文件,流水线保持文件驱动。
- 非 http(s) 的 doclobsUrl 视为本地挂载路径直接读。
- `extraParams`(可选 JSON 对象)原样存储以兼容旧调用,但不出现在回调/查询报文中,也不参与流程判定。
- 回调整篇一次,按 taskId;token 缓存 + 13012 刷新重试 + 13016 睡 60s 重试(≤5 次)。
- 出参报文(回调与 `GET /{docId}/result` 共用)见 [callback-contract.md](callback-contract.md):旧版扁平结构——`code/msg/docId/docKeyword/docType/docAbstract/taskId/applicable/applicableReason` 在顶层,条款结果全部放在 `data[]` 数组里。

## 6. 模块地图

```
api/                     FastAPI 路由(integration=后端面,其余内部)
clause_splitter/
  config.py              业务配置(并发/chunk 契约/ESG/调度,env 可覆盖)
  jobs.py                异步 JobRunner(队列+worker+完成钩子)
  orchestrator.py        agent/registry 装配点 + chain 编排
  rag/                   RAG 抽象层(base.py 抽象类 + providers/ 具体服务)—— 唯一检索依赖
  parsing/               外规 → blocks(远端优先,本地抽取兜底)
  agent/                 ReAct agent + ToolRegistry + calculator
  pipeline/              四阶段 stage + contexts + doc_profile(文档摘要/关键词)
  split/scheme.py        ①②层:方案推断/校验/应用
  split/chunker.py       按条分块(chunk_doc,CHUNK_* 契约)
  split/structure.py     ④层:analyze_structure
  compare/               headings(③层)/ loader / episode / ground / review / aggregate
  services/              integration_service(收数/回调)、library_service(内规库转发)、dispatcher(调度)、esg_client
  llm/                   provider(openai/pingan)+ registry
config/config_app.py     部署层配置(JOB_WORKERS/CALLBACK_URL…)
config/llm/*.yaml        模型配置(不入库)
```

## 7. 关键配置

| 配置 | 默认 | 说明 |
|---|---|---|
| `COMPARE_CONCURRENCY` | 2 | episode 并行度(= 内网 2 槽) |
| `JOB_WORKERS` | 1 | chain 串行,总在飞 LLM = 上述值 |
| `CHAIN_DISPATCH` / `CHAIN_CRON` | auto / `0 20 * * mon-sat` | 调度模式 / cron 表达式 |
| `CHUNK_LEVEL/SIZE/MIN_TOKENS` | 4 / 1500 / 50 | 按条分块契约(内外规共用) |
| `DOC_PROFILE_TIMEOUT_S` | 90 | 文档摘要/关键词单次 LLM 调用超时(失败走本地兜底) |
| `RAG_PROVIDER` / `RAG_API_BASE` | chillrag / 空 | provider 选型 / 服务地址;地址为空 = 未配置 |
| `RAG_API_USER` / `RAG_API_PASSWORD` | 空 | provider 登录凭据(不需要登录的 provider 忽略) |
| `RAG_CORPUS` / `RAG_PARSE_CORPUS` | clause_splitter_internal / clause_splitter_parse | 内规语料 / 外规解析临时语料 |
| `RAG_TIMEOUT_S` / `RAG_PARSE_TIMEOUT_S` | 30 / 1800 | 单次 HTTP / 上传解析入库轮询总时限(秒) |
| `PARSE_FALLBACK` | local | 远端解析失败时走本地纯文本抽取;`none` = 直接失败 |
| `ESG_*` | 空 | 网关四件套 + 回调路径;留空走 BACKEND_CALLBACK_URL 开发兜底 |
