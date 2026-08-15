# Clause Splitter — Clause Decomposition & Regulation Compliance Comparison

Splits legal / regulatory documents (**external regulations**) and a company's
internal rules (**internal regulations**) into compliance obligations, then —
at a **unified per-article (第X条) granularity** — determines how each external
clause is **covered** by the internal library (fully / partially / missing),
generates a professional gap-analysis report, and renders an **HTML review page**
showing the top-k internal clauses recalled for every external clause so 作业人员
can verify relevance.

Architecture: **CLI core + thin FastAPI shell**, a `DocumentStore` as the
single source of truth, and pluggable retrieval backends. The internal library
accepts **mixed source formats** (pdf / docx / pptx / images *and* multi-regulation
Excel workbooks) and supports **incremental index updates** + a **custom index
storage location**.

## Core capabilities

| Capability | Description |
|---|---|
| **Clause decomposition** | Splits a document into articles, decomposes each into atomic obligations (subject / action / target / condition), exports Excel + `structure.json` |
| **Regulation comparison** | Each external clause runs one ReAct agent episode; the LLM picks retrieval tools per turn |
| **Pluggable retrieval** | `grep` (ripgrep literal) / `bm25` (PySerini + jieba pretokenized) / `faiss` (qwen3-embedding dense) / `hybrid` (all three bound, LLM chooses) |
| **Unified per-article granularity** | Every 第X条 is one retrieval unit; chapters / sections are kept as `meta.section` context (「第一章　总则 > 第一条」). Heading depth is **adaptive**: 编/章/节/条 levels are assigned by which markers actually appear (no LLM, no fixed template) — a chapter-less regulation still splits per-article |
| **Mixed internal sources** | The internal library scans a directory of raw docs (pdf / docx / pptx / images) **and** Excel workbooks whose rows carry a 「制度名称」column — each 制度 in the workbook becomes its own document, its 第一条/第二条… each a retrieval unit |
| **Incremental index update** | Re-running `run_index.py` diffs source files by sha256: added docs are re-parsed + faiss-appended, modified docs force a full re-embed, unchanged docs reuse cached MinerU parse — no full rebuild needed (but `--rebuild` exists) |
| **Custom index location** | `--index-dir` / `INDEX_CACHE_DIR` relocates `.cache/<name>/{store, chunks, bm25, faiss, md}`; the store persists its own location so `run_compare` loads it back |
| **HTML review page** | After a compare, `内规检索复核.html` shows each external clause with its status and the top-k internal clauses recalled (制度名 / 条款标签 / 检索后端 / 得分 / keyword-highlighted snippet / expandable full text), with the adopted clause marked 「✓ 采用」 |
| **Anti-hallucination grounding** | Answer contract + trajectory hard validation: a claimed internal chunk id must come from the retrieval trace *and* exist in the store, otherwise the claim is dropped and the clause downgraded to *missing* |
| **Gap-analysis report** | LLM-generated Markdown (evidence quotes) with a deterministic template fallback |

## Architecture

```
CLI core (run_*.py) — all computation lives here
  ├─ clause_splitter/split/     clause decomposition (plain sequential pipeline)
  ├─ clause_splitter/compare/   comparison (episode + grounding + report)
  │    ├─ index.py      internal lib → chunks → store + 3 indexes (incremental, prepare_docs / load_store)
  │    ├─ episode.py    one external clause = one agent episode
  │    ├─ ground.py     anti-hallucination: claimed id ∈ trace ∪ store
  │    ├─ aggregate.py  batched parallel orchestration → CompareResult (+ top-k recall candidates)
  │    ├─ retrieve.py   deterministic batch pre-retrieval for the review page
  │    ├─ review.py     render the self-contained HTML 复核页 (内规检索复核.html)
  │    └─ report.py     gap-analysis report (LLM + template fallback)
  ├─ clause_splitter/agent/     ReActAgent (native OpenAI tool loop) + trace persistence
  ├─ clause_splitter/tools/     ToolRegistry (single truncation point)
  │    ├─ parser/       MinerU parse / Excel containers → blocks → hierarchy-aware chunker
  │    └─ searcher/     grep / bm25 / faiss (pluggable)
  └─ clause_splitter/llm/       LLM facade (openai / pingan, per-model YAML)
FastAPI shell (clause_splitter/api/) — upload → background task → poll → download
```

```
external doc  ──run_split──▶  clauses + structure.json
internal lib  ──run_index──▶  MinerU parse / Excel split → per-article chunks → DocumentStore
                               → grep / bm25 / faiss indexes (<root>/<name>/, incremental)
                                    │
external clauses ──run_compare──▶  ReAct agent (hybrid retrieval)
                                    → grounding → gap-analysis report
                                    → 内规检索复核.html (per-clause top-k internal recalls)
```

## Quick start

### Environment

```bash
uv venv && uv pip install -r requirements.txt
# .env is gitignored — never commit a real key
cp .env.example .env        # DASHSCOPE_API_KEY / JAVA_HOME(JDK21) / CORS_ORIGINS ...
```

Per-model LLM configs live in `config/llm/*.yaml`; keys always use `${ENV}`
placeholders (e.g. `api_key: ${DASHSCOPE_API_KEY}`) expanded at runtime.

### ① Split (standalone feature)

```bash
python run_split.py upload_materials/external/某风险评估办法.docx
# → outputs/<doc>/  <doc>_条款拆分结果.xlsx + <doc>_structure.json
# Multiple docs from an Excel summary sheet: python run_split.py 汇总表.xlsx
```

### ② Build the internal-library index (offline, idempotent, incremental)

```bash
python run_index.py --internal-dir upload_materials/internal/ --name internal
# scan every supported source (pdf / docx / pptx / xlsx containers / images)
# → MinerU parse (or Excel multi-regulation split) → per-article chunks → DocumentStore
# → faiss (if the embedding model is available) + BM25 Lucene indexes
# → .cache/internal/{md, store, chunks, bm25, faiss}/
```

**Incremental**: re-run the same command after adding / modifying a document — only
the new/changed file is re-parsed, new chunks are faiss-appended, unchanged files
reuse their cached parse. `--rebuild` forces a full re-parse + re-embed.

**Custom storage location**:

```bash
python run_index.py --internal-dir upload_materials/internal/ \
    --name internal --index-dir /mnt/shared/internal-idx/
# → /mnt/shared/internal-idx/internal/{store, chunks, bm25, faiss, md}/
# (env fallback: INDEX_CACHE_DIR=/mnt/shared/internal-idx)
```

### ③ Compare (core feature)

```bash
python run_compare.py --external <external doc/JSON/dir> --internal-dir upload_materials/internal/ \
    --searcher hybrid --output-dir outputs/compare/
# → compare_result.json (incl. top-k recall candidates)
# → 内外规合规差距分析报告.md (gap analysis)
# → 内规检索复核.html  ← open in a browser: per external clause, the top-k
#                          internal clauses recalled (制度名/条款标签/后端/得分/
#                          keyword-highlighted snippet/全文), adopted marked 「✓ 采用」
# → episodes/ (trace audit)
```

`run_compare` **loads the existing index** built by `run_index` (no re-parse); it
only falls back to building one when no index exists yet. Point it at a custom
location with the same `--index-dir` you used for `run_index`.


### ④ FastAPI shell

```bash
python run_api.py --port 8000
# /docs: POST /api/v1/split | POST /api/v1/compare | GET /api/v1/task/{id} ...
# CORS whitelist: CORS_ORIGINS="https://app.example.com" python run_api.py
```

## Retrieval backends

| Backend | Implementation | Best for |
|---|---|---|
| `grep` | ripgrep `-F` literal match, ranked by hit count | exact terms present in the internal rules |
| `bm25` | PySerini Lucene + **jieba pretokenized** (index `-pretokenized`, query `WhitespaceAnalyzer`) | multi-word combinations (subject + action + target) |
| `faiss` | sentence-transformers (`~/Models/Qwen3-Embedding-0.6B` by default; switch to bge-m3 by changing `EMBED_MODEL`) + IndexFlatIP | paraphrases, near-synonym phrasing |
| `hybrid` | all three bound, LLM picks per turn | default |

`run_index.py` builds all indexes; `run_compare --searcher` chooses which to bind.

## Parsing & chunking

The internal library handles **two very different source shapes** and unifies
both into one store at **per-article granularity**.

**Formal documents** (pdf / docx / pptx / images) are parsed by the **local
MinerU CLI** (`mineru -b pipeline`) into layout blocks. **Excel containers**
(an internal-rule workbook with one 制度 per group of rows) are split per
regulation by the 「制度名称」column — each 制度 becomes its own document and its
第一条/第二条… each a block; column mapping is heuristic + deterministic
(no LLM), so any layout with a recognizable 制度名称 / 条款内容 / 条款名称 column works.

Blocks then go through a **marker-driven heading pass** that is *adaptive* rather
than template-based: it scans which structural markers actually appear
(编 / 章 / 节 / 条), assigns **relative depth** (章-only doc → 章=1, 条=2;
编章节条 → 1/2/3/4; chapter-less doc → 条=1), and splits each 第X条 into its own
retrieval unit with the heading path preserved as `meta.section`
(「第一章　总则 > 第一条」). Real MinerU headings are respected and augmented;
documents with **no markers at all** (rare) fall back to MinerU's own heading
levels. Documents that only use 一、二、三、 numbering get a dunhao fallback.

| Knob | Default | Meaning |
|---|---|---|
| `MINERU_BACKEND` | `pipeline` | MinerU backend (`pipeline` / `vlm` / `hybrid`) |
| `CHUNK_LEVEL` | `4` | heading levels ≤ this open a new chunk; 4 = the deepest marker level, so every 第X条 is its own unit |
| `CHUNK_SIZE` | `1500` | soft token budget per chunk (oversized sections split, tables kept whole) |
| `CHUNK_MIN_TOKENS` | `50` | chunks below this merge into the previous same-section chunk |
| `REVIEW_TOP_K` | `10` | top-k internal clauses shown per external clause on the HTML review page |
| `INDEX_CACHE_DIR` | `.cache/` | relocates the derived index root (overridden by `--index-dir`) |

## HTML review page (内规检索复核.html)

The most important increment for 作业人员: every compare run also writes a
**self-contained HTML page** (inline CSS/JS, zero external resources — just open
it in a browser). For each external clause it shows the coverage status badge,
the external text, the analysis / remedy, and the **「检索召回 top-k」list**: the
internal clauses recalled across all bound backends (grep / bm25 / faiss merged by
chunk id, best score kept), each with a 制度名 badge, 条款标签, backend badge,
score, a snippet with query keywords `<mark>`-highlighted, and expandable full
text. The clause the episode actually adopted is marked **「✓ 采用」**.

Top-k buttons filter by status, a keyword search box filters across clauses and
snippets, and 全部展开 / 全部收起 collapse everything. The candidates are produced
by a **deterministic batch pre-retrieval** (`compare/retrieve.py`) that never
feeds back into the episode — pure display evidence for human review.

## Anti-hallucination design

Historical bug: "batch 0% coverage" — the agent claimed coverage while citing
internal ids that were never retrieved. Three layers of defense:

1. **Answer contract** — `matched_internal_clause_ids` may only reference
   `chunk_id`s returned by tools during the episode (`agent/prompts.py`).
2. **Hard grounding** — every claimed id must be in the retrieval trace's seen
   set *and* in the `DocumentStore`; any non-grounded claim is dropped.
3. **Downgrade** — if no claim survives grounding, the clause is downgraded to
   *missing* and the analysis replaced with a human-review notice.

Regression tests: `tests/test_grounding.py` (forged ids → missing).

## Repository layout

```
run_split.py / run_index.py / run_compare.py / run_api.py   # four CLI entry points
clause_splitter/
  ├─ config.py                 # single global config (one load_dotenv at import)
  ├─ models/                   # clause / compare / task data models
  ├─ llm/                      # LLM facade + providers (openai / pingan)
  ├─ agent/                    # ReActAgent + trace persistence + prompts
  ├─ tools/                    # ToolRegistry + parser/{blocks,mineru_parse,chunker,ingest}
  │                             #         + searcher/{grep,bm25,faiss,embed}
  ├─ split/                    # decomposition pipeline (sequential orchestration)
  ├─ compare/                  # index / episode / ground / aggregate / retrieve / review / report / loader
  ├─ api/                      # FastAPI thin shell
  ├─ export/  utils/  skills/  # Excel export / helpers / SKILL.md
config/llm/*.yaml              # per-model config (${ENV} placeholders)
```

## Usage scenarios

| Scenario | Command |
|---|---|
| **Internal library is one Excel with many regulations** | put it under `upload_materials/internal/`, run `run_index.py --name internal` — each 制度 (by 「制度名称」column) becomes a document, per-article chunks |
| **Mixed library** (a few word/pdf + an Excel) | all under `--internal-dir`; `run_index` routes each by extension automatically |
| **Added one new regulation** | drop the file into `--internal-dir`, re-run `run_index.py` — only the new file is parsed + faiss-appended, everything else reuses cache |
| **Modified a regulation** | overwrite the file, re-run `run_index.py` — its old parse cache is dropped, it is re-parsed + fully re-embedded (no stale vectors) |
| **Shared / network index location** | `run_index.py --index-dir /mnt/shared/idx`, then `run_compare.py --index-dir /mnt/shared/idx` loads the same store |
| **External regulation arrived — full remaining flow** | `run_compare.py --external 某外规.docx --output-dir outputs/外规比对/` → gap-analysis report + 内规检索复核.html |

## Tests

```bash
pytest tests/    # grounding anti-hallucination regression + per-article headings
                 # + Excel containers + incremental faiss append + review-page HTML
```

## Security baseline

- `.env` (real API keys) is gitignored — never commit it.
- `config/llm/*.yaml` uses `${ENV}` placeholders, never hardcoded keys.
- CORS uses a `CORS_ORIGINS` whitelist; `allow_credentials` is enabled only
  when the whitelist does not contain `*`.
