# Clause_Splitter 架构文档

条款拆分与外规内化分析服务:接收系统后端推送的外部法规,拆成条级条款,逐条与内规库比对判定内化覆盖,结果经 ESG 网关回调返回。算法层基于 vendored ChillRAG(混合检索 + ReAct grounding),业务编排全部在 `clause_splitter` 侧。

## 1. 总体架构

```
系统后端(平安 ESG)
   │ ① POST /api/v1/regulations/push          (docId/doclobsUrl/docContent/OptType…)
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
        或后端主动拉 GET /api/v1/regulations/{docId}/result
```

- **对外只有两个接口**(backend 面):`POST /regulations/push` 收数、`GET /regulations/{docId}/result` 拉结果;结果主动推送走 ESG 固定 callback。`/regulations/*` 其余路由、`/library`、`/jobs` 都是内部运维接口。
- 法规 = 拆分前的文档(后端 docId 对应),条款 = 拆分后的 第X条/Article 级单元。
- 单服务部署:算法与编排同进程,uvicorn 单 worker;重启恢复会把孤儿 queued/running 任务标 failed,scheduled 任务存活等下次 cron。

## 2. LLM 双通道与并发预算

| 通道 | Provider | 模型 | 用途 |
|---|---|---|---|
| 内网 | `pingan`(平安网关) | qwen3.8-27b,仅 2 并发槽 | 生产 |
| 外网 | `openai` | deepseek-v4-flash | 开发/冒烟 |

并发按 2 槽设计:episode 扇出 `COMPARE_CONCURRENCY=2`、chain 串行 `JOB_WORKERS=1`,最坏在飞 LLM 请求 = 2。模型配置在 `config/llm/<model_name>.yaml`(gitignore,example 入库),默认模型 `Settings.default_model = qwen3.8-27b`。

## 3. 拆条:四层阶梯(scheme ladder)

```
取 blocks:MinerU 后缀→ parse_docs(pdf/图/docx/pptx/xlsx)
           .md → parse_markdown_file;.txt/.doc → 逐行合成 blocks
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
- `structure.json` 记录 `split_layer ∈ {llm_scheme, builtin, llm_segment}` 与生效的 `scheme`,`split_mode` 同名返回。
- ① 层真实冒烟:英文 `Article N` 文档推出 `Article \d+` 方案;标准中文法规与 ③ 层输出等价。

## 4. 比对链(chain 四阶段)

| 阶段 | 行为 |
|---|---|
| applicability | LLM 判定法规是否适用于平安科技(手动推送 OptType=0 跳过,视为必适用) |
| split | 四层阶梯拆条,条款入库 |
| internalization | 逐条 LLM 判定条款类型/是否需要内化 |
| analysis | 逐条 ReAct episode:检索内规库(混合 BM25+faiss)→ grounding 证据 → 覆盖判定(完全/部分/缺失)+ 差异 + 整改建议 |

episode 语义:主谓宾拆解只作为分析期提示燃料,不改变条级拆分粒度。

## 5. 后端对接细节(对齐旧版)

- **doclobsUrl 优先**,docContent(str 入参)兜底;URL 下载失败且有 docContent → 降级落盘,无兜底 → 502。
- **魔数嗅探**:`%PDF-`→.pdf、`PK\x03\x04`→.docx、`D0CF11E0`→.doc、可 UTF-8 解码→.txt、其余 415;文件名 = 标题(attname 或 docId)+ 嗅探后缀。
- docContent 是 str 不是文件:`_materialize_text` 显式适配(`<br/>`→换行)写内部文件,流水线保持文件驱动。
- 非 http(s) 的 doclobsUrl 视为本地挂载路径直接读。
- 回调整篇一次,按 taskId;token 缓存 + 13012 刷新重试 + 13016 睡 60s 重试(≤5 次)。

## 6. 模块地图

```
api/                     FastAPI 路由(integration=后端面,其余内部)
clause_splitter/
  config.py              业务配置(并发/chunk 契约/ESG/调度,env 可覆盖)
  jobs.py                异步 JobRunner(队列+worker+完成钩子)
  orchestrator.py        ChillRAG 引擎移交点(seam)
  pipeline/              四阶段 stage + contexts
  split/scheme.py        ①②层:方案推断/校验/应用/文本合成 blocks
  split/structure.py     ④层:analyze_structure
  compare/               headings(③层)/ loader / episode / review / aggregate
  services/              integration_service(收数/回调)、dispatcher(调度)、esg_client
  llm/                   provider(openai/pingan)+ registry
chillrag/                vendored 通用 RAG 引擎(不为本项目改动)
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
| `ESG_*` | 空 | 网关四件套 + 回调路径;留空走 BACKEND_CALLBACK_URL 开发兜底 |
