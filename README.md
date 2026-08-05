# Clause Splitter — 条款结构分析与内外规比对

将法律/监管文件（外规）与企业内部规章制度（内规）智能拆解为原子化的合规义务记录，通过 BM25 语义检索 + LLM ReAct Agent 进行内外规比对，并自动生成专业的差距分析报告。

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
      │                          │  Lucene BM25 索引   │
      │                          │  (万级内规秒级检索)  │
      │                          └─────────┬──────────┘
      │                                    │
      └──────────┬─────────────────────────┘
                 ▼
      ReAct Compare Agent (LLM + BM25 Tools)
      每条外规义务 → BM25 搜索内规库 → 审查候选 → 判定
      (完全覆盖 / 部分覆盖 / 缺失)
                 │
                 ▼
        差距分析报告 (Markdown)
```

### 三步流水线

| 阶段 | 说明 | 输入 | 输出 |
|---|---|---|---|
| **Split** — 条款拆分 | 智能识别文档结构，原子化提取每条合规义务 | PDF/DOCX/DOC/TXT/XLSX/XLS | JSON + Excel |
| **Compare** — 内外规比对 | 外规义务与内规义务语义匹配，BM25 检索 + ReAct Agent | 外规JSON + 内规JSON | 比对结果JSON |
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

- **BM25 + ReAct Agent** — PySerini Lucene BM25 检索引擎 + LLM ReAct 搜索代理
- **万级内规秒级检索** — Lucene 倒排索引，10,000 条内规 < 200ms 构建，BM25 毫秒级查询
- **内存 Obligation Store** — O(1) 详情检索，无需 Lucene I/O
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

### 安装

```bash
pip install -r requirements.txt
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

### 端到端测试

```bash
# ── 任务 1: 拆分全部内规 ──────────────────────
python run.py --split --file upload_materials/内部网络数据安全管理条例.docx

# ── 任务 2: 外规 vs 全部内规 → 差距分析报告 ────
# Step 1: 拆分外规
python run.py --split --file upload_materials/网络数据安全风险评估办法.docx

# Step 2: 一比多比对 + 报告
python run.py --summary-comparison \
  --external-files outputs/网络数据安全风险评估办法/网络数据安全风险评估办法_split_result.json \
  --internal-dir outputs/
```

> `--internal-dir` 自动扫描目录下所有 `*_split_result.json`，内规库扩充后只需把新 JSON 放入同一目录。

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

### Compare 流水线（BM25 + ReAct Agent）

```
外规义务 (N条)
      │
      ▼
  分批 (15条/批)
      │
      ▼
  asyncio.gather 并行 ─────────────────────┐
      │                                     │
      ▼                                     ▼
  ReAct Agent (每批)                  ReAct Agent
      │                                     │
      ├─ bm25_search(query)                 │
      ├─ get_obligation_detail(id)          │
      ├─ search_by_fields(...)              │
      │   (最多5轮迭代)                     │
      ▼                                     ▼
  MatchResult[]                      MatchResult[]
      │                                     │
      └──────────────┬──────────────────────┘
                     ▼
              合并 → CompareResult
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
│       ├── compare_agent.py        ReAct Compare Agent
│       └── compare_state.py        CompareState 定义
│
├── pipeline/                       管线编排
│   └── compare_pipeline.py         BM25 索引 + 分批并行比对
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
│   │   └── __init__.py             ReAct Agent 提示词
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
| `matched_internal_id` | 匹配到的内规义务序号（缺失时为 null） |
| `analysis` | 匹配理由或缺失点分析 |
| `remedy_suggestion` | 整改建议 |

## 版本历史

| 版本 | 日期 | 主要变更 |
|---|---|---|
| 0.2.0 | - | BM25 + ReAct 比对、Excel 输入、prompts 模块化、一对多比对 |
| 0.1.5 | - | 比对+报告一体化接口、进度追踪优化 |
| 0.1.0 | - | 初始版本：条款拆分 + Excel 导出 |
