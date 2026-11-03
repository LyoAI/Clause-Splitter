# Clause Splitter — 外规内化 · 单条差异分析(算法模块)

对**一条已拆好的外规条款**做内化判定,并在内规库(Zeus)中检索比对,输出外规内化结论。
工程侧负责遍历条款、拆条、调度与 ESG 回调;本仓库只维护这一段算法。

接口与字段契约见 [assets/clause-analysis-interface.md](assets/clause-analysis-interface.md);
与其他功能分支合并时的差异清单见 [assets/merge-notes.md](assets/merge-notes.md)。

## 用法

```python
from clause_analysis import ClauseAnalyzer, ClauseInput

analyzer = ClauseAnalyzer.from_config()          # 启动时构建一次
result = await analyzer.analyze(
    ClauseInput(
        term_name="第十二条",
        term_content="网络运营者应当……",
        term_id="",                                # 可选,透传到出参 termId
        document_name="网络数据安全风险评估办法",   # 可选
        section="第三章 风险评估内容",              # 可选
    ),
    need_internalization=True,   # 外规(docType=1);内规/附件(2/3)传 False:只提关键词
    keyword_mode="rules",        # 关键词提取:rules(默认,正则) | llm(失败自动回退 rules)
)
element = result.to_esg()                        # data[] 元素中的算法侧字段
```

单条冒烟(真实 LLM + Zeus,需先配置 `.env` 与 `config/llm/<model>.yaml`;
设置 `RUN_ENV=stg|prd` 时改为读 `.{RUN_ENV}.env` 与 `config/llm/<model>-<RUN_ENV>.yaml`):

```bash
uv run python scripts/analyze_clause.py examples/clause_input.json
```

## 结构

```
clause_analysis/          本模块的全部领域逻辑(domain)
  analyzer.py             统一入口 ClauseAnalyzer.analyze(): 判定 → 比对 → 透传
  judge.py                内化判定(需要内化 / 无需内化),只看条款原文
  episode.py              比对 episode 的 user prompt(只含条款原文)
  grounding.py            解析 <answer>,verdict 原样透传
  types.py                ClauseInput / ClauseResult / to_esg()
  config.py               本模块配置(环境变量)
  prompts/                提示词(markdown)
llm/                      LLM facade + openai / pingan provider      (共享)
rag/                      RagService 接口 + Zeus 客户端(仅检索)      (共享)
agent/                    通用 ReAct 循环 + 工具注册                   (共享)
utils/                    logger 与文本工具                            (共享)
config/llm/*.yaml         模型配置
scripts/analyze_clause.py 单条冒烟脚本
tests/                    离线单测(纯逻辑 + Zeus 请求构造)
```

## 设计要点

- **单条为操作单元**,无状态:每次 `analyze` 独立,检索缓存只属于本次调用。
- **关键词前置提取**:内外规条款都要提关键词;`rules` 正则(默认,毫秒级)或 `llm`(质量更好,失败自动回退 rules),按耗时预算逐次选。
- **内规/附件短路**:`need_internalization=False`(docType 2/3)只产出身份字段 + 关键词,不判定、不检索,不要求配置 Zeus。
- **不做五元组拆解**:episode 只读条款原文,不让模型在输出中先拆义务。
- **判定透传**:覆盖结论不因引用 id 无法回查而改变;id 只回填展示字段。
- **降级而非抛出**:单条超时/解析失败/检索异常降级为 `缺失` + 需人工复核;仅 Zeus 鉴权/未配置会抛出,由调用方决定是否中止整批。
- **检索范围按次传**:`analyze(clause, search_labels=...)` 由调用方指定 Zeus 标签范围,不传则不加过滤、检索整个索引;范围不在客户端初始化时写死。

## 测试

```bash
uv run pytest -q tests
```

测试不使用假件服务:纯逻辑直接测;Zeus 与 LLM 的真实联通用 `scripts/analyze_clause.py` 验证。
