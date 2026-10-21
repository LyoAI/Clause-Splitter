# Clause Splitter — Regulation Internalization Service

A FastAPI service that internalizes external regulations (外规) against a
company's internal-rules library (内规). Given an external regulation document,
it runs a four-stage pipeline and exposes every intermediate result over HTTP
so a system backend can drive stage by stage (human approval steps sit between
stages in the product flow):

1. **Applicability** (document-level) — does this regulation apply to
   平安科技? Fail-open: any LLM/parse error ⇒ "需人工复核", never a silent "no".
2. **Split (拆条)** — deterministic MinerU → heading → per-article chunking,
   with an LLM segmentation fallback.
3. **Internalization judgment** — per article: 需要内化 / 无需内化 with reason.
4. **Analysis** — per internalizable article: a **ReAct LLM agent over hybrid
   retrieval** (ripgrep / BM25 / faiss) produces obligation decomposition,
   coverage status (完全覆盖 / 部分覆盖 / 缺失), difference analysis, core
   regulatory requirements, remediation suggestions, evidence-grounded
   internal-clause matches and top-k retrieval candidates; then a gap-analysis
   report (Markdown) and a self-contained HTML review page are rendered.

Answer grounding is strict: a claimed internal chunk id must appear in the
episode's retrieval trace *and* exist in the store, otherwise the claim is
dropped and the clause downgraded to 缺失.

## Layout

```
api/                    FastAPI app (PingAn http-server-template style)
  base_fastapi.py       app, {code,status,data,msg} envelope, request_id, loguru
  deps.py               auth + serializers        routes/{regulations,library,jobs}.py
clause_splitter/
  pipeline/             the 4 stages + chain orchestration (thin over engines)
  jobs/runner.py        in-process asyncio job runner (queue, workers, recovery)
  services/             regulation / library use-cases between routes and repos
  db/repo.py            MongoDB (motor) repositories, one class per collection
  storage.py            DATA_DIR layout helpers + artifact allowlist
  prompts/              ALL LLM prompts as editable .md files ({{slot}} render)
  agent/ split/ compare/ tools/   engines — ReAct agent, split, compare, retrieval
common/                 loguru / mongo_async / cyberark (template shared code)
config/                 config_app (service knobs) · config_env · config_mongo
tests/                  pytest suite — repo / stages / runner / full-chain e2e (offline)
serving.py              entry point (port 18998, PingAn deployment convention)
```

Storage layout under `DATA_DIR` (default `data/`):

```
data/
├── regulations/{id}/ source/ · split/structure.json · episodes/*.json
│                      compare_result.json · 内外规合规差距分析报告.md · 内规检索复核.html
└── internal/  docs/  ·  index/internal/{store, chunks, bm25, faiss, md}
```

## API (mounted under `/api/v1`)

All responses use the `{code, status, data, msg}` envelope; set `API_KEY` to
require `X-API-Key` on every route.

```
POST   /regulations                        multipart upload (pdf/docx/doc/txt/md), sha256 dedupe
GET    /regulations · GET /regulations/{id} · DELETE /regulations/{id}
PATCH  /regulations/{id}/applicability     human override {applicable, reason}
POST   /regulations/{id}/applicability | split | internalization | analysis | run
                                           → 202 {job} (run = full chain 1→4)
GET    /regulations/{id}/clauses?decision=&status=&include=candidates
GET    /regulations/{id}/clauses/{clause_id}
GET    /regulations/{id}/jobs · GET /jobs?state=&job_type= · GET /jobs/{id}
GET    /regulations/{id}/artifacts · GET /regulations/{id}/artifacts/{name}
POST   /library/documents · GET /library/documents · DELETE /library/documents/{id}
POST   /library/index {rebuild?} · GET /library/index
POST   /integration/push                 backend doc push (docContent or doclobsUrl; OptType=manual
                                           skips stage 1) → auto-runs the chain
GET    /healthz
```

Full request/response reference: `assets/API.md`. When `BACKEND_CALLBACK_URL`
is set, chain jobs started by a push POST a result payload back to the backend
(draft contract, awaiting alignment).

Stage preconditions are enforced with 409: split needs applicability done
(`{"force": true}` overrides a "not applicable" verdict), internalization needs
split, analysis needs internalization. One active job per regulation; re-running
a stage resets it and all downstream stages (no mixed generations). Chain runs
stages inline with progress weights 0.10 / 0.15 / 0.25 / 0.50 and skips
downstream stages when the regulation is judged not applicable (or needs human
review).

## Retrieval

The agent picks tools per turn from a pluggable registry:
`grep` (ripgrep) / `bm25` (PySerini + jieba) / `faiss` (qwen3-embedding dense) /
`hybrid`. The internal library accepts raw docs (pdf / docx / pptx / images) and
multi-regulation Excel workbooks (「制度名称」 column); indexing is incremental
by sha256. Analysis jobs pin a store snapshot under a read lock while an index
rebuild takes the writer lock.

## Run

```bash
# local
pip install -r requirements.txt
python serving.py                     # 0.0.0.0:18998

# docker (PingAn intranet; needs the sidecar detector)
docker build -t clause-splitter . && bash docker_cmd.sh
```

Copy `.env.example` → `.env` for LLM keys and service knobs. Key settings
(env-overridable): `DATA_DIR`, `MONGO_*`, `API_KEY`, `JOB_WORKERS`,
`COMPARE_CONCURRENCY`, `SEARCH_TOP_K`, `REVIEW_FILTER_RELEVANCE`,
`COMPANY_PROFILE`, `MINERU_BACKEND`, `EMBED_MODEL`.

## Development

```bash
.venv/bin/python -m pytest tests/ -q    # 141 tests, fully offline (fake LLM + in-memory Mongo)
```

The e2e test drives the real chain (upload → applicability → split → judge →
episodes → report → review HTML) against a scripted LLM and a deterministic
substring searcher — no network, no MinerU, no MongoDB.

Notes: run uvicorn with 1 worker (in-process queue + local index); MinerU is a
heavy subprocess used by the deterministic split path; the Docker base image
must be Python ≥ 3.12 (`requires-python`).
