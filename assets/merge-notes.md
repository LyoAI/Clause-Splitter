# 分支合并说明 · feature/clause-analysis

给合并代码时看的差异清单:本分支相对 `main` 改了什么、其他分支合并进来时要跟着改什么。
调用契约(工程侧怎么调本模块)见 [clause-analysis-interface.md](clause-analysis-interface.md);本文只讲"合并"。

## 1. 本分支的定位

- 只保留流程⑩的算法:**单条已拆好的外规条款 → 内化判定 + Zeus 内规检索比对 → ESG 结论字段**。
- 拆条、适用性、文档摘要、调度、落库、ESG 回调**都不在本分支**——工程分支与拆条分支各自实现。
- 操作单元是单条款:`analyze()` 无状态,工程侧写 for 循环逐条调用,并发 ≤ 2。

## 2. 目录布局变化(合并冲突的主要来源)

| main | 本分支 |
|---|---|
| `clause_splitter/` 包壳,所有模块在其下 | 包壳删除;共享模块提升到仓库根:`agent/` `llm/` `rag/` `utils/` |
| `clause_splitter/compare/` `pipeline/` `split/` `parsing/` … | 领域逻辑集中到新增的 `clause_analysis/` |
| `clause_splitter/prompts/` | `clause_analysis/prompts/`(仅保留 internalization / analysis_agent / force_answer) |
| 外层 `clause_splitter/config.py` | 删除;配置只有 `config/llm/*.yaml` + `clause_analysis/config.py` + `.env` |

import 路径随之变化:`from clause_splitter.rag import X` → `from rag import X`(llm / agent / utils 同理)。

## 3. 共享模块接口变化(合并后调用方要跟着改)

| 模块 | main | 本分支 |
|---|---|---|
| `rag.base.RagService` | 7 个抽象方法(parse / ingest / search / fetch_chunks / documents / stats / delete) | 只剩 `available` + `search(query, k, labels=None)`;`labels` **按次传**,None/空 = 不过滤、检索整个索引 |
| `rag.providers.zeus.ZeusProvider` | 全功能,构造参数含 `search_labels`(初始化写死) | 仅检索;构造无 `search_labels`;`doc_id` 取行内 `org_data_id`(= 后端 docId),**不再解析 `doc:` 标签** |
| `rag.providers.chillrag` | 本地开发 provider | 删除,只留 zeus |
| `rag.types` | Chunk / Hit 等多个类型 | 只剩 Chunk / Hit;`Chunk.with_doc()` 删除(工具输出直接用 `chunk.meta`) |
| `agent.registry.ToolRegistry` | knowledge_search + calculate | 只有 `knowledge_search`(calculator 删除);构造加 `default_labels`(检索范围,由 analyze 传入);`failed_searches` 计数保留;相同 query 重复检索被拦截并计数(`duplicate_searches`) |
| `llm.get_llm` / `LLM` | — | 加 `config_dir` / `timeout_s` 参数(openai / pingan provider 同步) |
| `utils` | — | 纯移动,无内容变化 |

## 4. 已删除的能力(合并时不要"恢复"回来)

- 工程侧文件:`api/`、`services/`、`jobs.py`、`db.py`、`storage.py`、`orchestrator.py`、`models.py`、`templates/`、`Dockerfile` 等——这些归工程分支维护;若工程分支从 main 切出,合并时这些文件会出现在冲突里,**以工程分支为准**,不要从本分支恢复。
- 算法侧明确不要的:`agent/calculator.py`(不需要计算)、`chillrag` provider、五元组拆解(`prompts/obligations.md` 及"先拆义务再比对"的流程)。
- `ZEUS_SEARCH_LABELS` 环境变量:已删除,检索范围改由 `analyze(clause, search_labels=[...])` 按次传入。内网 `.env` 里这行可以删掉。

## 5. 对接约定(其他分支需要遵守)

- **入库侧(工程)**:内规入库时 `org_data_id` 必须传**后端 docId**——检索命中的 `relationDocId` 直接取它;`text_labels` 必打(实测不打标签检索不到),标签清单与工程侧对齐。
- **调用侧(工程 for 循环)**:每条款一次 `analyze(ClauseInput(...), need_internalization=..., keyword_mode=..., search_labels=...)`;`need_internalization` 按 docType 映射(1 外规 → True;2 内规 / 3 附件 → False,只提关键词、分析字段全空);`keyword_mode` 按耗时预算选(`rules` 默认 / `llm`);并发 ≤ 2(内网 LLM 只有 2 个槽);错误语义见接口文档 §4——仅 Zeus 鉴权/未配置会抛出,其余单条降级不中断整批。
- **出参**:`to_esg()` 含条款身份字段(`termId` / `termContent` / `termKeyword` / `termName`)+ 分析结果字段,枚举为数字字符串。`termId` / `termContent` / `termName` 为入参原样回传(入参已由 `clause_number` / `text` 改名为 `term_name` / `term_content`);`termKeyword` 由独立前置关键词提取阶段产出,内外规条款都要提取,`relationTerms[].termKeyword` 同规则填充(rules 正则;llm 模式由比对阶段顺带提取);内规/附件路径分析字段全空(`internalize=""`,区别于判定产生的 `"0"`/`"1"`)。工程侧仍需补齐 `chapter` / `orderNum` 等文档级字段。入参必填只有 `term_content` / `term_name`。

## 6. 冲突处理建议

- **目录布局**:以仓库根目录的共享模块为准,把对方对 `clause_splitter/*` 的改动迁移过来,再删旧路径。
- **`rag/base.py` / `zeus.py` / `agent/registry.py`**:以本分支为基底;对方新增的能力按新接口接回(如需入库/解析能力,应作为独立 provider 或服务,不要复活 RagService 的旧方法)。
- **工程文件(api / jobs / db / services 等)**:以工程分支为准。
- **prompt**:逐条对照;算法语义以 `clause_analysis/prompts/` 为准。
- **`assets/callback-contract.md`**:main 全流程时代的出参旧稿,仅作历史参考;算法字段以 `clause-analysis-interface.md` 与正式 docx 为准。
