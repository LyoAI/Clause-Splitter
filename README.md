# Clause Splitter — 条款结构分析与内外规比对

将法律/监管文件（外规）与企业内部规章制度（内规）智能拆解为原子化的合规义务记录，通过语义匹配进行内外规比对，并自动生成专业的差距分析报告。

## 核心业务流程

```
外规文档 (PDF/DOCX)                 内规文档 (PDF/DOCX)
      │                                    │
      ▼                                    ▼
  条款拆分                         条款拆分
  (LangGraph 流水线)               (LangGraph 流水线)
      │                                    │
      ▼                                    ▼
  义务记录 (JSON)                   义务记录 (JSON)
      │                                    │
      └──────────┬─────────────────────────┘
                 ▼
           LLM 语义比对
      (完全覆盖 / 部分覆盖 / 缺失)
                 │
                 ▼
        差距分析报告 (Markdown)
```

### 三步流水线

| 阶段 | 说明 | 输入 | 输出 |
|---|---|---|---|
| **Split** — 条款拆分 | 智能识别文档结构，原子化提取每条合规义务 | PDF/DOCX/DOC/TXT | JSON + Excel |
| **Compare** — 内外规比对 | 外规义务与内规义务语义匹配，判定覆盖状态 | 外规JSON + 内规JSON | 比对结果JSON |
| **Summary** — 报告生成 | 基于比对结果生成专业差距分析报告 | 比对结果 + 原文 | Markdown 报告 |

## 功能特性

### 条款拆分（Split）

- **多格式文档解析** — 支持 PDF、DOCX、DOC、TXT 四种格式
- **三级结构分析策略** — LLM 优先识别 → 正则兜底匹配 → 全文平铺兜底，保证任何格式都能处理
- **复杂度自动分级** — 评估条款复杂度，简单条款 One-shot LLM 快速处理，复杂条款 ReAct 多轮推理
- **原子化义务提取** — 一个动词 = 一条独立记录，不合并多个动作
- **上下文自动补全** — 分句提取时自动补全主语/条件，确保每条记录语义完整
- **多层委托结构识别** — 正确判定"成员公司须指定统筹部门，承担……"中"指定"和"承担"的不同主体
- **歧义标注** — 存在多种解读时自动标注两种可能的理解及选择理由
- **定义/列举过滤** — 自动识别名词解释和范围列举类条款，标记为"不拆分"
- **Excel 模板导出** — 严格按业务模板13列格式输出

### 内外规比对（Compare）

- **语义级别匹配** — 不依赖关键词，使用 LLM 理解条款深层含义进行匹配
- **三级覆盖判定** — 每条外规义务判定为 **完全覆盖 / 部分覆盖 / 缺失**
- **逐条分析说明** — 每条匹配结果附带匹配理由或缺失点分析
- **整改建议** — 针对每个差距给出具体的内规修订建议
- **多对多支持** — 支持多个外规 vs 多个内规同时比对
- **分批处理** — 每20条外规分批调用LLM，避免上下文溢出
- **来源追踪** — 每条匹配结果可追溯到原始文档和条款编号

### 差距分析报告（Summary）

- **专业报告结构** — 摘要 → 核心发现 → 关键风险点 → 深度差距分析 → 结论与行动建议
- **按主题分组** — 将相关差距自动归类（如"评估机构管理体系"、"监管报送机制"等）
- **风险优先级** — 标注每个差距的整改优先级（高/紧急）
- **制度完善建议** — 指出需要新增或修订的具体内规章节

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

### 使用示例

```bash
# 1. 仅拆分外规
curl -X POST http://localhost:8000/api/v1/split \
  -F "file=@网络数据安全风险评估办法.docx" \
  -F "document_name=网络数据安全风险评估办法"

# 2. 拆分完成后，比对 + 生成报告
curl -X POST http://localhost:8000/api/v1/summary_comparison \
  -F "external_files=@外规拆分结果.json" \
  -F "internal_files=@内规拆分结果.json"

# 3. 一键流水线
curl -X POST http://localhost:8000/api/v1/pipeline \
  -F "files=@网络数据安全风险评估办法.docx" \
  -F "internal_files=@内部网络数据安全管理条例_split_result.json"

# 4. 查询进度
curl http://localhost:8000/api/v1/task/{task_id}

# 5. 下载报告
curl -O http://localhost:8000/api/v1/result/{task_id}/report
```

## 数据处理流水线

### Split 流水线（LangGraph 状态图）

```
parse_document → analyze_structure → plan_batches
                                        │
                          ┌─────────────┴─────────────┐
                          ▼                           ▼
                   decompose_simple            decompose_complex
                   (One-shot LLM)              (ReAct 多轮推理)
                          │                           │
                          └─────────────┬─────────────┘
                                        ▼
                                    validate
                                   ┌─ ── ── ─┐
                                   │ self_correct │ (最多3次重试)
                                   └─ ── ── ─┘
                                        ▼
                                  export_excel
```

**各节点职责**：

| 节点 | 职责 | 策略 |
|---|---|---|
| `parse_document` | 将 PDF/DOCX 转为纯文本 | 多引擎解析 |
| `analyze_structure` | 识别章节、条款边界和复杂度 | LLM优先 → 正则兜底 → 平铺兜底 |
| `plan_batches` | 按复杂度规划处理批次 | 复杂条款单独成批，简单条款合并 |
| `decompose_simple` | 拆分简单条款 | One-shot LLM + YAML 结构化输出 |
| `decompose_complex` | 拆分复杂条款 | ReAct 模式多轮推理 |
| `validate` | 校验拆分结果 | Pydantic 验证 + 完整性检查 |
| `self_correct` | 自动修正 | 将校验错误反馈给 LLM 重新生成 |
| `export_excel` | 导出 Excel | 复制模板 → 逐行填充 → 保留样式 |

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

## 运行示例

以下为实际运行结果（外规：《网络数据安全风险评估办法》↔ 内规：《内部网络数据安全管理条例》）：

### 拆分统计

| 文档 | 条款数 | 提取义务数 |
|---|---|---|
| 网络数据安全风险评估办法（外规） | 25 条 | 63 条 |
| 内部网络数据安全管理条例（内规） | 20 条 | 84 条 |

### 比对覆盖分布

| 覆盖状态 | 数量 | 占比 |
|---|---|---|
| 完全覆盖 | 0 条 | 0% |
| 部分覆盖 | 14 条 | 22.6% |
| 缺失 | 28 条 | 45.2% |

### 报告核心发现

1. **评估机构管理体系全面缺失** — 未规定评估机构的行为准则、报告真实性责任、禁止转委托、信息删除及保密要求
2. **监管报送与整改机制真空** — 重要数据处理者未被要求按时报送评估报告、配合核验、限期整改
3. **特殊场景合规遗漏** — 核心数据处理者专项评估义务缺失，商用密码应用安全性评估完全空白
4. **禁止重复评估机制缺失** — 不同监管部门可能就同一事件重复要求评估，增加企业合规成本

## 技术架构

```
main.py                    FastAPI 应用入口
├── api/routes.py          REST API 端点定义
├── api/dependencies.py    依赖注入（LLM、TaskManager）
├── agent/                 LangGraph 流水线
│   ├── graph.py           状态图构建 + 条件路由
│   ├── state.py           AgentState（TypedDict）
│   ├── nodes/             7个处理节点
│   └── prompts/           提示词模板（拆分/比对/报告/校验/复杂度判定）
├── services/              业务服务层
│   ├── splitter.py        条款拆分服务
│   ├── compare_service.py 内外规比对服务
│   ├── summary_service.py 报告生成服务
│   └── task_manager.py    异步任务管理
├── models/                Pydantic 数据模型
│   ├── clause.py          ClauseObligation, ClauseSplitResult
│   ├── compare.py         MatchResult, CompareResult
│   ├── task.py            TaskStatus
│   └── api.py             Request/Response 模型
├── export/
│   └── excel_exporter.py  Excel 模板导出器
├── llm/                   LLM 适配层
│   ├── pingan_llm_serving.py    PingAn LLM
│   └── openai_langchain_llm_serving.py  OpenAI 兼容接口
├── config/settings.py     全局配置（阈值/正则/里程碑）
├── skills/                业务规范（SKILL.md）
│   ├── split/             拆分规范
│   ├── compare/           比对规范
│   └── summary/           报告生成规范
└── outputs/               输出目录（样例结果）
```

### 技术栈

| 类别 | 技术 |
|---|---|
| Web 框架 | FastAPI + Uvicorn |
| 工作流引擎 | LangGraph（StateGraph + MemorySaver） |
| LLM | PingAn LLM Serving / OpenAI 兼容接口（默认 qwen3-max） |
| 文档解析 | pdfplumber + python-docx + docx2txt |
| Excel | openpyxl（模板复制样式策略） |
| 数据校验 | Pydantic v2 |
| 异步 | FastAPI BackgroundTasks |

## 快速开始

### 环境要求

- Python >= 3.12

### 安装

```bash
# 安装依赖
pip install -r requirements.txt

# 配置 LLM（编辑 .env 文件）
# 示例（OpenAI 兼容接口）：
#   LLM_BASE_URL=https://your-llm-endpoint/v1
#   LLM_API_KEY=your-api-key
#   LLM_MODEL=qwen3-max
```

### 启动服务

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

启动后访问：
- API 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

### 运行测试

```bash
pytest test_app.py -v
```

## 版本历史

| 版本 | 日期 | 主要变更 |
|---|---|---|
| 0.2.0 | - | 多对多比对支持、pipeline 一键流水线 |
| 0.1.5 | - | 比对+报告一体化接口、进度追踪优化 |
| 0.1.0 | - | 初始版本：条款拆分 + Excel 导出 |
