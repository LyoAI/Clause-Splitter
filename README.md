# Clause Splitter — 条款拆分与内外规比对智能体

将法律/监管文件（**外规**）与企业内部规章制度（**内规**）拆解为合规义务记录，按**条款**粒度判断每条外规条款在内规中的承接情况（完全覆盖 / 部分覆盖 / 缺失），并自动生成专业差距分析报告。

参考 [FinIndicator-Bench](https://github.com/example/FinIndicator-Bench) 的三层管线模式重构：CLI 核心 + FastAPI 薄壳、`DocumentStore` 单一数据源、`BaseSearcher` 可插拔检索。

## 核心能力

| 能力 | 说明 |
|---|---|
| **条款解构** | 把法规文档拆成章节/条款结构，逐条分解为原子化合规义务（主体/动作/事项/条件），导出 Excel + `structure.json` |
| **内外规比对** | 以**条款**为检索单元（`chunk_id = "制度名#条款号"`），每条外规条款一个 ReAct episode，LLM 自主选择检索工具 |
| **可插拔检索** | `grep`（ripgrep 字面）/ `bm25`（PySerini + jieba 预分词）/ `faiss`（qwen3-embedding 稠密向量）/ `hybrid`（三工具全绑，LLM 每轮自选） |
| **防幻觉 grounding** | 答案契约 + 轨迹硬校验：agent 引用的内规条款 ID 必须来自检索轨迹且存在于条款库，否则降级为缺失 |
| **差距分析报告** | LLM 按 `skills/summary/SKILL.md` 生成专业 Markdown 报告（证据原文引用），失败时确定性模板回退 |

## 架构

```
CLI 核心（run_*.py）── 计算全部在此
  ├─ clause_splitter/split/    条款拆分（普通顺序编排，无 LangGraph）
  ├─ clause_splitter/compare/  条款级比对（episode + grounding + 报告）
  │    ├─ index.py     内规库 → 条款级 DocumentStore + 三索引
  │    ├─ episode.py   每条外规条款 = 一个 agent episode
  │    ├─ ground.py    防幻觉：claimed id ∈ 轨迹 ∪ 条款库
  │    ├─ aggregate.py batch 并行编排 → CompareResult
  │    └─ report.py    差距分析报告（LLM + 模板回退）
  ├─ clause_splitter/agent/    ReActAgent（OpenAI 原生工具循环）+ 轨迹落盘
  ├─ clause_splitter/tools/    ToolRegistry（单点截断）+ searcher（可插拔）
  └─ clause_splitter/llm/      LLM facade（openai / pingan，按 yaml 配置）
FastAPI 薄壳（clause_splitter/api/）── 上传 → 后台任务 → 轮询 → 下载
```

```
外规文档 ──run_split──▶ 条款 + structure.json
内规库   ──run_index──▶ DocumentStore + grep/bm25/faiss 索引（.cache/，幂等复用）
                           │
外规条款 ──run_compare──▶ ReAct agent（hybrid 检索）→ grounding → 差距分析报告
```

## 快速开始

### 环境

```bash
uv venv && uv pip install -r requirements.txt
# .env 配置（.env 已在 .gitignore，禁止提交真实 key）
cp .env.example .env        # DASHSCOPE_API_KEY / JAVA_HOME(JDK21) / CORS_ORIGINS ...
```

`config/llm/*.yaml` 的 key 一律用 `${ENV}` 占位（如 `api_key: ${DASHSCOPE_API_KEY}`），运行时从环境展开。

### ① 拆分（独立功能）

```bash
python run_split.py upload_materials/external/网络数据安全风险评估办法.docx
# → outputs/<文档>/  <文档>_条款拆分结果.xlsx + <文档>_structure.json
# Excel 多文档：python run_split.py 汇总表.xlsx
```

### ② 建内规索引（一次，之后幂等复用）

```bash
python run_index.py --internal-dir outputs/internal/ --name internal
# → .cache/internal/{chunks,bm25,faiss,store}/  内容不变即复用
```

### ③ 比对（核心）

```bash
python run_compare.py --external <外规doc/JSON/目录> --internal-dir outputs/internal/ \
    --searcher hybrid --output-dir outputs/compare/
# → compare_result.json + 内外规合规差距分析报告.md + episodes/（轨迹审计）
```

### ④ FastAPI 薄壳

```bash
python run_api.py --port 8000
# /docs 可用：POST /api/v1/split | POST /api/v1/compare | GET /api/v1/task/{id} ...
# CORS 白名单：CORS_ORIGINS="https://app.example.com" python run_api.py
```

## 检索后端

| 后端 | 实现 | 适用 |
|---|---|---|
| `grep` | ripgrep `-F` 字面匹配，按命中数排序 | 内规中出现的确定词 |
| `bm25` | PySerini Lucene + **jieba 预分词**（索引 `-pretokenized`，查询 `WhitespaceAnalyzer`） | 多词组合（主体+动作+事项） |
| `faiss` | sentence-transformers（默认 `~/Models/Qwen3-Embedding-0.6B`，可换 bge-m3）+ IndexFlatIP | 语义改写、近义表述 |
| `hybrid` | 三者全绑，LLM 每轮自选工具 | 默认模式 |

`SearcherType` / `SEARCHER_TYPE` 配置切换；`run_index.py` 构建全部索引，`run_compare --searcher` 选择使用哪些。

## 防幻觉设计

历史 bug："批量 0% 覆盖" —— agent 声称已覆盖但引用的内规 ID 不在检索结果里。三道防线：

1. **答案契约**：`matched_internal_clause_ids` 只允许引用轨迹内返回过的 `chunk_id`（`agent/prompts.py` 铁律）。
2. **grounding 硬校验**：每个 claimed id 必须 ∈ 检索轨迹 seen 集 且 ∈ `DocumentStore`；任一不落地即丢弃。
3. **降级**：覆盖性判定若没有任何可接地 claim → 整体降级为缺失，analysis 替换为降级文案，提示人工复核。

回归测试：`tests/test_grounding.py`（伪造 ID → 缺失）。

## 目录结构

```
run_split.py / run_index.py / run_compare.py / run_api.py   # 四个 CLI 入口
clause_splitter/
  ├─ config.py                 # 全局配置 + 阈值（load_dotenv 一处生效）
  ├─ models/                   # clause / compare / task 数据模型
  ├─ llm/                      # LLM facade + provider（openai / pingan）
  ├─ agent/                    # ReActAgent + 轨迹持久化 + prompts
  ├─ tools/                    # ToolRegistry + searcher/{grep,bm25,faiss,embed}
  ├─ split/                    # 拆分管线（顺序编排）
  ├─ compare/                  # index / episode / ground / aggregate / report / loader
  ├─ api/                      # FastAPI 薄壳
  ├─ export/  utils/  skills/  # Excel 导出 / 工具 / SKILL.md
config/llm/*.yaml              # 模型配置（${ENV} 占位）
```

## 测试

```bash
pytest tests/    # grounding 防幻觉回归 + jieba BM25 集成
```

## 安全基线

- `.env`（含真实 API key）已在 `.gitignore`，禁止提交。
- `config/llm/*.yaml` 用 `${ENV}` 占位，不硬编码密钥。
- CORS 用 `CORS_ORIGINS` 白名单；`allow_credentials` 仅在白名单不含 `*` 时开启。
