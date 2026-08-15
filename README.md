# Clause Splitter — Clause Decomposition & Regulation Compliance Comparison

Splits legal / regulatory documents (**external regulations**) and a company's
internal rules (**internal regulations**) into compliance obligations, then —
at a deliberately coarse, section-level granularity — determines how each
external clause is **covered** by the internal library (fully / partially /
missing) and generates a professional gap-analysis report.

Architecture: **CLI core + thin FastAPI shell**, a `DocumentStore` as the
single source of truth, and pluggable retrieval backends.

## Core capabilities

| Capability | Description |
|---|---|
| **Clause decomposition** | Splits a document into articles, decomposes each into atomic obligations (subject / action / target / condition), exports Excel + `structure.json` |
| **Regulation comparison** | Each external clause runs one ReAct agent episode; the LLM picks retrieval tools per turn |
| **Pluggable retrieval** | `grep` (ripgrep literal) / `bm25` (PySerini + jieba pretokenized) / `faiss` (qwen3-embedding dense) / `hybrid` (all three bound, LLM chooses) |
| **Coarse internal granularity** | Internal rules are indexed by MinerU parse → hierarchy-aware chunks (heading-level, ~1500 tokens), **not** per-obligation — so comparison retrieval units stay coarse |
| **Anti-hallucination grounding** | Answer contract + trajectory hard validation: a claimed internal chunk id must come from the retrieval trace *and* exist in the store, otherwise the claim is dropped and the clause downgraded to *missing* |
| **Gap-analysis report** | LLM-generated Markdown (evidence quotes) with a deterministic template fallback |

## Architecture

```
CLI core (run_*.py) — all computation lives here
  ├─ clause_splitter/split/     clause decomposition (plain sequential pipeline)
  ├─ clause_splitter/compare/   comparison (episode + grounding + report)
  │    ├─ index.py      internal library → chunks → store + 3 indexes (prepare_docs)
  │    ├─ episode.py    one external clause = one agent episode
  │    ├─ ground.py     anti-hallucination: claimed id ∈ trace ∪ store
  │    ├─ aggregate.py  batched parallel orchestration → CompareResult
  │    └─ report.py     gap-analysis report (LLM + template fallback)
  ├─ clause_splitter/agent/     ReActAgent (native OpenAI tool loop) + trace persistence
  ├─ clause_splitter/tools/     ToolRegistry (single truncation point)
  │    ├─ parser/       MinerU parse → blocks → hierarchy-aware chunker
  │    └─ searcher/     grep / bm25 / faiss (pluggable)
  └─ clause_splitter/llm/       LLM facade (openai / pingan, per-model YAML)
FastAPI shell (clause_splitter/api/) — upload → background task → poll → download
```

```
external doc  ──run_split──▶  clauses + structure.json
internal lib  ──run_index──▶  MinerU parse → chunks → DocumentStore
                               → grep / bm25 / faiss indexes (.cache/, idempotent)
                                    │
external clauses ──run_compare──▶  ReAct agent (hybrid retrieval)
                                    → grounding → gap-analysis report
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

### ② Build the internal-library index (offline, idempotent)

```bash
python run_index.py --internal-dir outputs/internal/ --name internal
# MinerU parses each source doc → hierarchy-aware chunks → DocumentStore
# → faiss (if the embedding model is available) + BM25 Lucene indexes
# → .cache/internal/{md, store, chunks, bm25, faiss}/
```

### ③ Compare (core feature)

```bash
python run_compare.py --external <external doc/JSON/dir> --internal-dir outputs/internal/ \
    --searcher hybrid --output-dir outputs/compare/
# → compare_result.json + gap-analysis report .md + episodes/ (trace audit)
```

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

The internal library is parsed by the **local MinerU CLI** (`mineru -b pipeline`),
projected onto layout blocks, then grouped by heading hierarchy — a heading at
`level <= CHUNK_LEVEL` (default 3) opens a new chunk, deeper sub-headings stay
inside the current chunk, so granularity is **coarse on purpose**: a
regulation corpus is indexed at chapter/section level, not per obligation.

| Knob | Default | Meaning |
|---|---|---|
| `MINERU_BACKEND` | `pipeline` | MinerU backend (`pipeline` / `vlm` / `hybrid`) |
| `CHUNK_LEVEL` | `3` | heading levels ≤ this open a new chunk |
| `CHUNK_SIZE` | `1500` | soft token budget per chunk (oversized sections split, tables kept whole) |
| `CHUNK_MIN_TOKENS` | `50` | chunks below this merge into the previous same-section chunk |

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
  ├─ compare/                  # index / episode / ground / aggregate / report / loader
  ├─ api/                      # FastAPI thin shell
  ├─ export/  utils/  skills/  # Excel export / helpers / SKILL.md
config/llm/*.yaml              # per-model config (${ENV} placeholders)
```

## Tests

```bash
pytest tests/    # grounding anti-hallucination regression + jieba BM25 integration
```

## Security baseline

- `.env` (real API keys) is gitignored — never commit it.
- `config/llm/*.yaml` uses `${ENV}` placeholders, never hardcoded keys.
- CORS uses a `CORS_ORIGINS` whitelist; `allow_credentials` is enabled only
  when the whitelist does not contain `*`.
