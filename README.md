# Clause Splitter — 条款结构分析与内外规比对

将法律/监管文件（外规）与企业内部规章制度（内规）智能拆解为原子化的合规义务记录，通过「BM25 确定性召回最相关候选 → LLM 逐条判定承接」进行内外规比对，并自动生成专业的差距分析报告。

## 核心业务流程

```
外规文档 (PDF/DOCX/XLSX)              内规文档 (PDF/DOCX/XLSX)
      │                                    │
      ▼                                    ▼
  条款拆分                              条款拆分
  (LangGraph 流水线)                    (LangGraph 流水线)
      │                                    │
      ▼                                    ▼
  义务记录 (JSON)                    义务记录 (JSON)
      │                                    │
      │                          ┌─────────▼──────────┐
      │                          │  🔧 离线构建索引     │  ← 一次性操作
      │                          │  Lucene BM25 索引   │
      │                          │  (万级内规秒级检索)  │
      │                          └─────────┬──────────┘
      │                                    │
      │                          ┌─────────▼──────────┐
      │                          │  🚀 在线复用索引     │  ← 每次比对直接打开
      │                          │  毫秒级加载，不重建  │
      │                          └─────────┬──────────┘
      │                                    │
      └──────────┬─────────────────────────┘
                 ▼
      BM25 召回候选（确定性，无 LLM）
      每条外规义务 → 检索 top-K 内规候选
                 │
                 ▼
      LLM 逐条判定（候选表 → 行号）
      (完全覆盖 / 部分覆盖 / 缺失)
                 │
                 ▼
        差距分析报告 (Markdown)
```

### 三步流水线

| 阶段 | 说明 | 输入 | 输出 |
|---|---|---|---|
| **Split** — 条款拆分 | 智能识别文档结构，原子化提取每条合规义务 | PDF/DOCX/DOC/TXT/XLSX/XLS | JSON + Excel |
| **Compare** — 内外规比对 | 外规义务与内规义务语义匹配，BM25 召回候选 + LLM 逐条判定 | 外规JSON + 内规JSON | 比对结果JSON |
| **Summary** — 报告生成 | 基于比对结果生成专业差距分析报告 | 比对结果 + 原文 | Markdown 报告 |

## 功能特性

### 条款拆分（Split）

- **多格式文档解析** — 支持 PDF、DOCX、DOC、TXT、XLSX、XLS 六种格式
- **Excel 结构化解析** — ReAct Agent 自动检测列映射，支持多文档 Excel（一个文件含多部法规）
- **三级结构分析策略** — LLM 优先识别 → 正则兜底匹配 → 全文平铺兜底
- **复杂度自动分级** — 启发式评估条款复杂度，简单条款批量 One-shot LLM，复杂条款 ReAct 多轮推理
- **原子化义务提取** — 一个动词 = 一条独立记录，不合并多个动作
- **上下文自动补全** — 分句提取时自动补全主语/条件
- **Excel 模板导出** — 严格按业务模板13列格式输出

### 内外规比对（Compare）

- **离线索引构建** — 内规拆分完成后，一次性构建 Lucene BM25 索引到 `outputs/internal/.bm25_index/`
- **在线复用索引** — 每次外规比对时直接打开已有索引，毫秒级加载，无需重建
- **索引重建** — 内规库变更后，运行 `--build-index --force` 全量重建（< 1秒）
- **jieba 词级分词** — 中文按词切分（`数据安全` / `管理制度`），比默认的单字/双字切分精度更高；分词器版本记录在 `index_manifest.json`，更换分词器会自动触发全量重建
- **确定性召回 → 逐条判定** — 对每条外规义务：先用结构化字段 + 完整原文构建查询，BM25 确定性召回 top-K 内规候选（无 LLM 参与检索），再由 LLM 基于候选表逐条判定承接（完全覆盖 / 部分覆盖 / 缺失），每条义务一次独立 LLM 调用，天然支持并行，也杜绝了 ReAct 大批次会话的信息过载与幻觉
- **万级内规秒级检索** — Lucene 倒排索引，10,000 条内规 < 200ms 构建，BM25 毫秒级查询
- **内存 Obligation Store** — O(1) 详情检索，无需 Lucene I/O
- **持久化义务库** — 构建索引时一并把全部内规义务按行持久化为 `obligations.jsonl`（每行一条义务，追加/流式加载均高效），比对时服务端直接从此加载原文，**无需重新上传内规库**（build once → analyze many）；进程内缓存 + 并发请求共享一份只读 store
- **候选带完整原文** — 召回结果直接携带内规条款**完整原文**（非摘要），以 Markdown 表格呈现给 LLM；匹配到的内规出处（文档名 + 条款号 + 原文）由校验层逐字回填，供差距分析报告直接引用
- **行号判定防幻觉** — LLM 输出候选表**行号**而非内规ID（结构上无法编造）；凡声称覆盖却无法落到任一真实候选的判定，自动降级为缺失并替换分析文本，杜绝编造的"内规条款引用"进入报告
- **三级覆盖判定** — 每条外规义务判定为 **完全覆盖 / 部分覆盖 / 缺失**
- **三级覆盖判定** — 每条外规义务判定为 **完全覆盖 / 部分覆盖 / 缺失**
- **逐条分析 + 整改建议** — 每条匹配附带分析理由和具体修订建议
- **一对多比对** — 一个外规 vs 整个内规数据库，通过 `--internal-dir` 一键指定
- **分批并行处理** — asyncio.gather 并发处理，Semaphore 限流

### 差距分析报告（Summary）

- **专业报告结构** — 摘要 → 核心发现 → 关键风险点 → 深度差距分析 → 结论与行动建议
- **风险优先级** — 标注每个差距的整改优先级（高/紧急）

## 快速开始

### 环境要求

- Python >= 3.12
- **Java 21**（BM25 Lucene 检索引擎依赖，通过 PySerini 调用）
  ```bash
  brew install openjdk@21
  ```
- **jieba**（中文词级分词，纯 Python 无编译依赖，内网离线机器可从外网拷贝 wheel 传输）
  ```bash
  pip install jieba
  ```

### 安装

```bash
pip install -r requirements.txt   # 已包含 jieba
```

### 配置

编辑 `.env` 文件（已内置开发环境默认值）：

```bash
# LLM API
DASHSCOPE_API_KEY=your-api-key

# PySerini BM25 依赖（内网部署时按实际路径修改）
JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home
OPENAI_API_KEY=dummy
```

所有环境变量均由 `.env` 自动加载，无需手动 export。

### 启动服务

```bash
uv run main.py
# 或: uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

启动后访问：
- API 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

### CLI 使用指南（run.py）

> **哪些命令需要服务端？**
> - **本地直接执行**（无需启动服务）：`--analyze`、`--analyze-index`、`--build-index`、`--index-info`、`--list`、`--clear`
> - **需要服务端在跑**（`uv run main.py`）：`--split`、`--pre-split`、`--compare`

```bash
# ═══════════════════════════════════════════════════════
# ① 离线准备（仅一次）：拆分内规 + 构建索引
# ═══════════════════════════════════════════════════════

# Excel 内规库按制度名拆分成多份 → 每份进 outputs/internal/
python run.py --pre-split --file 内规库.xlsx --output-dir outputs/internal

# 拆单个文件（多文件用逗号分隔）
python run.py --split --file 内规A.docx --output-dir outputs/internal
python run.py --split --file 内规A.docx,内规B.pdf --output-dir outputs/internal

# 构建 BM25 索引（一次性；之后新增内规会自动增量追加）
python run.py --build-index --internal-dir outputs/internal/

# 分词器变更 / 想全量重建时加 --force
python run.py --build-index --internal-dir outputs/internal/ --force

# ═══════════════════════════════════════════════════════
# ② 在线比对：新外规 vs 内规库（每次新外规，只传外规文件）
# ═══════════════════════════════════════════════════════

# 建好索引后，内规库由服务端从持久化索引加载（build once → analyze many）。
# 每次来一份新外规，只需传入外规文件，无需再上传内规：
#   外规是原始文档（pdf/docx/txt）→ 自动拆分后再比对；
#   外规已是 *_split_result.json → 直接比对。
python run.py --compare --external 外规.docx --output-dir outputs/external

# 分步执行（外规已拆好时直接比，省一次拆分）
python run.py --split --file 外规.docx --output-dir outputs/external
python run.py --compare \
  --external outputs/external/外规/外规_split_result.json

# 也可显式指定内规（覆盖索引库，用于自定义子集比对）
python run.py --compare --external 外规.docx --internal-dir outputs/internal/ --output-dir outputs/external
python run.py --compare \
  --external outputs/external/外规/外规_split_result.json \
  --internal-files 内规1.json,内规2.json

# ═══════════════════════════════════════════════════════
# ③ 索引管理（本地，无需服务端）
# ═══════════════════════════════════════════════════════

# 看索引状态（多少条、多大）
python run.py --index-info --internal-dir outputs/internal/

# 看索引实际用的分词器版本 + 术语样例
python run.py --analyze-index --internal-dir outputs/internal/

# 排查：一条查询会被切成哪些词（为什么搜不到）
python run.py --analyze "网络数据出境安全管理"

# ═══════════════════════════════════════════════════════
# ④ 内规变更后的索引维护
# ═══════════════════════════════════════════════════════

# 1. 重新拆分变更的内规文档
python run.py --split --file 修改后的内规.docx --output-dir outputs/internal

# 2. 重建索引（全量，< 1秒）
python run.py --build-index --internal-dir outputs/internal/ --force

# ═══════════════════════════════════════════════════════
# ⑤ 任务记录管理
# ═══════════════════════════════════════════════════════

python run.py --list            # 列出所有任务
python run.py --list split      # 按类型筛选
python run.py --clear           # 清空任务记录
```

> **索引策略**: Lucene 索引是 append-only 的，不支持增量删除。内规变更后推荐全量重建，数千条义务 < 1 秒完成。未加 `--force` 时，`--build-index` 会通过 `index_manifest.json` 智能追加**新增**的制度文档；若检测到索引是用旧分词器构建的（manifest 中 `analyzer` 字段不同），会自动转为全量重建，避免不同分词方式混在一个索引里导致检索失真。
>
> **持久化义务库**: 构建索引的同时，全部内规义务会原子写入 `<index>/obligations.jsonl`（每行一条：`{"id": "制度名::序号", ...完整义务字段}`）。建好索引后，比对只需传入外规文件，内规库自动从索引加载（build once → analyze many）。**传入的内规库才是比对对象**：若调用方显式传入的内规与持久化索引不完全一致（不同/子集/重拆分的制度），流水线会从传入集合临时建索引进行比对，绝不使用陈旧的持久化索引；只有两者逐条完全一致时才复用持久化索引（毫秒级）。旧版单文件 `obligations.json` 仍可读取，下次构建索引会自动迁移为 JSONL。

### 运行单元测试

```bash
pytest tests/ -v
```

## API 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/split` | 上传法规文件，提交拆分任务 |
| `POST` | `/api/v1/summary_comparison` | 上传外规+内规JSON，提交比对+报告任务 |
| `POST` | `/api/v1/pipeline` | 一键运行完整流水线（拆分→比对→报告） |
| `GET` | `/api/v1/task/{task_id}` | 查询任务状态与进度 |
| `GET` | `/api/v1/tasks` | 列出所有任务 |
| `GET` | `/api/v1/result/{task_id}/json` | 获取 JSON 结果 |
| `GET` | `/api/v1/result/{task_id}/excel` | 下载 Excel 拆分结果 |
| `GET` | `/api/v1/result/{task_id}/report` | 下载 Markdown 差距分析报告 |
| `DELETE` | `/api/v1/tasks` | 清空所有任务记录 |
| `GET` | `/health` | 健康检查 |

### curl 示例

```bash
# 1. 仅拆分外规
curl -X POST http://localhost:8000/api/v1/split \
  -F "file=@网络数据安全风险评估办法.docx"

# 2. 比对 + 报告（可多文件）
curl -X POST http://localhost:8000/api/v1/summary_comparison \
  -F "external_files=@外规拆分结果.json" \
  -F "internal_files=@内规1.json" \
  -F "internal_files=@内规2.json"

# 3. 一键流水线
curl -X POST http://localhost:8000/api/v1/pipeline \
  -F "files=@外规.docx" \
  -F "internal_files=@内规.json"

# 4. 查询进度 / 下载
curl http://localhost:8000/api/v1/task/{task_id}
curl -O http://localhost:8000/api/v1/result/{task_id}/report
```

## 数据处理流水线

### Split 流水线（LangGraph 状态图）

```
parse_document → analyze_structure → plan_batches
    │                                     │
    ├─ Excel: parse_excel ────────────────┘
    │                                     │
    │               ┌─────────────────────┴──────────────┐
    │               ▼                                    ▼
    │        decompose_simple                    decompose_complex
    │        (批量 One-shot LLM)                 (ReAct 多轮推理)
    │               │                                    │
    │               └──────────────┬─────────────────────┘
    │                              ▼
    │                           validate
    │                         ┌─ ── ── ─┐
    │                         │ self_correct │ (最多3次重试)
    │                         └─ ── ── ─┘
    │                              ▼
    └──────────────────────► export_excel
```

### Compare 流水线（确定性召回 → 逐条判定 → 证据校验）

```
┌─ 离线（一次性）──────────────────────────────────────┐
│  outputs/internal/**/*_split_result.json             │
│      │                                               │
│      ▼                                               │
│  IndexManager.build_from_directory()                 │
│      │                                               │
│      ▼                                               │
│  outputs/internal/.bm25_index/  ← Lucene BM25 索引   │
│  + obligations.jsonl（义务库，含完整原文）            │
└──────────────────────────────────────────────────────┘

┌─ 在线（每次比对）────────────────────────────────────┐
│  外规义务 (N条)                                      │
│       │                                              │
│       ▼                                              │
│  asyncio.gather 并行（Semaphore 限流）──┐            │
│       │                                │            │
│       ▼                                ▼            │
│  ① 召回（确定性，无 LLM）              …            │
│     build_retrieval_queries()                     │
│     （结构化字段 + 完整原文 构建查询）               │
│     → retrieve_candidates() → top-K 候选           │
│       │                                            │
│       ▼                                            │
│  ② 判定（LLM，一次调用/条）                        │
│     候选 Markdown 表 → 输出 status + 行号           │
│       │                                            │
│       ▼                                            │
│  ③ 校验（validation.ground_match）                 │
│     行号→真实内规义务，逐字回填原文/出处             │
│     声称覆盖却无有效行号 → 降级缺失                 │
│       │                                            │
│       ▼                                            │
│  MatchResult × N → CompareResult                   │
└──────────────────────────────────────────────────────┘
```

## 项目结构

```
clause_splitter/
├── main.py                         FastAPI 应用入口
├── run.py                           E2E 测试客户端
├── api/
│   ├── routes.py                   REST API 端点
│   └── dependencies.py             DI（LLM, TaskManager）
│
├── agent/                          LangGraph 拆分管线
│   ├── graph.py                    状态图构建 + 条件路由
│   ├── state.py                    AgentState
│   ├── nodes/                      处理节点
│   │   ├── parse_document.py       PDF/DOCX/DOC/TXT 解析
│   │   ├── parse_excel.py          Excel ReAct 解析
│   │   ├── analyze_structure.py    文档结构分析
│   │   ├── plan_batches.py         批次规划
│   │   ├── decompose_simple.py     简单条款批量拆分
│   │   ├── decompose_complex.py    复杂条款 ReAct 拆分
│   │   ├── validate.py             校验 + 自修正
│   │   └── export_excel.py         Excel 导出
│   └── tools/                      工具函数
│       ├── decompose_tools.py      拆分工具
│       ├── excel_parse_tools.py    Excel 解析工具
│       ├── structure_tools.py      结构分析工具
│       ├── utility_tools.py        复杂度估算等
│       └── tools_registry.py       工具注册
│
├── operators/                      处理算子
│   └── compare/
│       ├── retrieval.py            确定性 BM25 候选召回（无 LLM）
│       ├── judge.py                逐条 LLM 承接判定（候选表 → 行号）
│       └── validation.py           证据回填 + 幻觉降级
│
├── pipeline/                       管线编排
│   └── compare_pipeline.py         BM25 索引 + 逐条并行 召回→判定→校验
│
├── tools/                          工具层
│   └── search/
│       ├── base.py                 BaseSearcher 抽象
│       ├── bm25_searcher.py        BM25Searcher (PySerini)
│       └── index_manager.py        Lucene 索引管理
│
├── prompts/                        提示词（按场景模块化）
│   ├── loader.py                   PromptLoader
│   ├── split/                      拆分提示词
│   │   ├── structure.py
│   │   ├── decompose_simple.py
│   │   ├── decompose_complex.py
│   │   ├── validate.py
│   │   └── excel_parse.py
│   ├── compare/
│   │   └── judge.py                逐条承接判定提示词（候选表 → 行号）
│   └── summary/
│       └── __init__.py             报告生成提示词
│
├── skills/                         业务规范（SKILL.md）
│   ├── split/SKILL.md
│   ├── compare/SKILL.md
│   └── summary/SKILL.md
│
├── services/                       业务服务层
│   ├── splitter.py                 拆分服务
│   ├── parser.py                   文档解析服务
│   ├── summary_service.py          报告生成服务
│   └── task_manager.py             异步任务管理
│
├── models/                         Pydantic 数据模型
│   ├── clause.py                   ClauseObligation
│   ├── compare.py                  MatchResult, CompareResult
│   ├── task.py                     TaskStatus
│   └── api.py                      Request/Response
│
├── config/
│   └── settings.py                 全局配置
│
├── export/
│   └── excel_exporter.py           Excel 模板导出
│
├── llm/                            LLM 适配层
│   ├── simple_llm.py               简单工厂
│   └── providers/                  PingAn / OpenAI
│
├── utils/
│   ├── log/logger.py               日志
│   └── file/                       文件工具
│
├── outputs/                        默认输出目录
├── tests/                          单元测试
└── .env                            环境变量（JAVA_HOME, API_KEY）
```

### 技术栈

| 类别 | 技术 |
|---|---|
| Web 框架 | FastAPI + Uvicorn |
| 工作流引擎 | LangGraph（StateGraph + MemorySaver） |
| LLM | OpenAI 兼容接口（默认 qwen3-max） |
| BM25 检索引擎 | PySerini 0.44 + Lucene (Anserini fatjar) |
| Java 运行时 | OpenJDK 21 |
| 文档解析 | pdfplumber + python-docx + docx2txt + openpyxl |
| Excel | openpyxl（模板复制样式策略） |
| 数据校验 | Pydantic v2 |
| 异步 | FastAPI BackgroundTasks + asyncio |

## 数据模型

### ClauseObligation（义务记录，13字段）

| 列 | 字段 | 说明 |
|---|---|---|
| A | 序号 | 行号 |
| B | 触发义务条件 | 时间/地点/场景/前提条件 |
| C | 触发义务子条件 | 子条件（如有） |
| D | 义务主体 | 执行动作的机构或角色 |
| E | 义务客体 | 动作指向的对象 |
| F | 义务规则 | `应当` 或 `禁止` |
| G | 义务动作 | 单个谓语动词 |
| H | 义务事项 | 动词指向的具体业务内容 |
| I | 实现义务方式 | 如何履行义务 |
| J | 文件名称 | 来源文档 |
| K | 条款编号 | 条款引用，如"第十四条" |
| L | 引用原文 | 最短完整原句 |
| M | 歧义说明 | 无歧义 / 具体歧义解释 |

### MatchResult（匹配结果）

| 字段 | 说明 |
|---|---|
| `external_clause_id` | 外规义务序号 |
| `status` | `完全覆盖` / `部分覆盖` / `缺失` |
| `matched_internal_id` | 匹配到的内规义务ID（缺失时为 null，格式 `制度名::序号`） |
| `matched_internal_document_name` | 匹配到的内规文档名 |
| `matched_internal_clause_number` | 匹配到的内规条款号（如"第十四条"） |
| `matched_internal_source_text` | 匹配到的内规义务**完整原文**（报告引用证据） |
| `analysis` | 匹配理由或缺失点分析 |
| `remedy_suggestion` | 整改建议 |

## 版本历史

| 版本 | 日期 | 主要变更 |
|---|---|---|
| 0.3.0 | - | 比对改为「确定性 BM25 召回 → 逐条 LLM 判定 → 证据校验」，修复大批次 ReAct 幻觉导致的 0% 覆盖；CLI 精简为 `--compare` |
| 0.2.0 | - | BM25 比对、Excel 输入、prompts 模块化、一对多比对 |
| 0.1.5 | - | 比对+报告一体化接口、进度追踪优化 |
| 0.1.0 | - | 初始版本：条款拆分 + Excel 导出 |
