# Clause Splitter — Regulation Internalization Service

A FastAPI service that internalizes external regulations (外规) against a
company's internal-rules library (内规). Given an external regulation document,
it runs a four-stage pipeline and exposes every intermediate result over HTTP
so a system backend can drive stage by stage (human approval steps sit between
stages in the product flow):

1. **Applicability** (document-level) — does this regulation apply to
   平安科技? Fail-open: any LLM/parse error ⇒ "需人工复核", never a silent "no".
2. **Split (拆条)** — remote parse (full text / markdown, with a local
   plain-text fallback) → heading detection → per-article chunking, with an LLM
   segmentation fallback.
3. **Internalization judgment** — per article: 需要内化 / 无需内化 with reason.
4. **Analysis** — per internalizable article: a **ReAct LLM agent over the
   external RAG service** produces coverage status (完全覆盖 / 部分覆盖 / 缺失),
   difference analysis, core regulatory requirements, remediation suggestions,
   evidence-grounded internal-clause matches and top-k retrieval candidates;
   a self-contained HTML review page is then rendered.

Answer grounding is strict: a claimed internal chunk id must appear in the
episode's retrieval trace *and* be resolvable from that run's retrieval cache,
otherwise the claim is dropped and the clause downgraded to 缺失.

## Layout

```
api/                    FastAPI app (PingAn http-server-template style)
  base_fastapi.py       app, {code,status,data,msg} envelope, request_id, loguru
  deps.py               auth + serializers        routes/{regulations,library,jobs}.py
clause_splitter/
  rag/                  external RAG abstraction (RagService) + providers/ (the only retrieval dep)
  parsing/              source document → blocks (remote parse first, local text fallback)
  agent/                in-repo ReAct agent + tool registry over the RAG service
  split/                split ladder (LLM scheme → headings → chunker) + per-article chunker
  compare/              episodes, grounding, aggregation, review page
  pipeline/             the 4 stages + chain orchestration
  services/             regulation / library / integration use-cases
  jobs.py               in-process asyncio job runner (queue, workers, recovery)
  db.py                 MongoDB (motor) repositories, one class per collection
  storage.py            DATA_DIR layout helpers + artifact allowlist
  prompts/              ALL LLM prompts as editable .md files ({{slot}} render)
common/                 loguru / mongo_async / cyberark (template shared code)
config/                 config_app (service knobs) · config_env · config_mongo
tests/                  pytest suite — repo / stages / runner / full-chain e2e (offline)
serving.py              entry point (port 18998, PingAn deployment convention)
```

Storage layout under `DATA_DIR` (default `data/`):

```
data/
├── regulations/{id}/ source/ · split/structure.json · episodes/*.json
│                      compare_result.json · 内规检索复核.html
└── internal/docs/     uploaded internal documents (mirrored into the RAG service)
```

## API (mounted under `/api/v1/clause_splitter`)

All responses use the `{code, status, data, msg}` envelope, except
`GET /regulations/{docId}/result`, which returns the flat backend result
payload directly (identical to the ESG callback body). Set `API_KEY` to
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
POST   /regulations/push                 backend doc push (docContent or doclobsUrl; optType=manual
                                           skips stage 1) → auto-runs the chain
GET    /healthz
```

Full request/response reference: `assets/API.md`. When `BACKEND_CALLBACK_URL`
is set, chain jobs started by a push POST a result payload back to the backend;
the payload is aligned with the legacy flat structure (document fields at the
top level, clauses in `data[]`) — see `assets/callback-contract.md`.

Stage preconditions are enforced with 409: split needs applicability done
(`{"force": true}` overrides a "not applicable" verdict), internalization needs
split, analysis needs internalization. One active job per regulation; re-running
a stage resets it and all downstream stages (no mixed generations). Chain runs
stages inline with progress weights 0.10 / 0.15 / 0.25 / 0.50 and skips
downstream stages when the regulation is judged not applicable (or needs human
review).

## Retrieval (external RAG service)

Parsing, chunking, indexing and search all run in an **external HTTP service**.
Business code depends only on the `RagService` interface
(`clause_splitter/rag/base.py`); the configured implementation is a provider
(`RAG_PROVIDER`: `chillrag` for local development, `zeus` for the intranet
knowledge base) and owns every wire detail. ChillRAG uses
`RAG_API_BASE` (local dev: `http://127.0.0.1:9022`) plus `RAG_API_USER` /
`RAG_API_PASSWORD`; Zeus uses `RAG_API_BASE` plus `ZEUS_APP_ID` /
`ZEUS_APP_SECRET` / `ZEUS_INDEX` (see `assets/rag-service-contract.md`).

- Split sends the document to the provider's `parse`; when the service is
  unavailable or unsupported, `PARSE_FALLBACK=local` extracts plain text locally
  (pdfplumber / python-docx / openpyxl / md / txt). `structure.json` records the
  chosen path as `parse_source`. Zeus returns no parsed text (`supports_parse =
  False`), so external regulations always parse locally under that provider.
- Internal-library uploads forward to `ingest`; Mongo keeps a mirror row (remote
  doc id, chunk count, state). `POST /library/index` re-ingests every stored
  file — also the recovery path after a service outage. Zeus indexes in the
  background and exposes no status endpoint, so the mirror stores an optimistic
  `indexed` state and `chunk_count=0` (unknown).
- The agent picks per turn from `knowledge_search`, `calculate` and — when the
  provider supports them — `get_context` / `get_materials` (Zeus has no
  per-document chunk or listing endpoints, so both are hidden). Grounding
  resolves claimed chunk ids from the run's retrieval cache.

To target a different service, add a provider under
`clause_splitter/rag/providers/` and register it in `_PROVIDERS`; business code,
split and output shape stay unchanged. See `assets/rag-service-contract.md`.

## Run

```bash
# local (uv; creates .venv from uv.lock)
uv sync --extra dev
uv run python serving.py              # 0.0.0.0:18998

# docker (PingAn intranet; needs the sidecar detector)
docker build -t clause-splitter . && bash docker_cmd.sh
```

Create a `.env` (gitignored) with service knobs only; LLM credentials live in
`config/llm/<model>.yaml`. Key settings
(env-overridable): `DATA_DIR`, `MONGO_*`, `API_KEY`, `JOB_WORKERS`,
`COMPARE_CONCURRENCY`, `SEARCH_TOP_K`,
`DOC_PROFILE_TIMEOUT_S`, `COMPANY_PROFILE`, `RAG_PROVIDER`, `RAG_API_BASE`,
`RAG_API_USER`, `RAG_API_PASSWORD`, `RAG_CORPUS`, `RAG_PARSE_CORPUS`,
`RAG_TIMEOUT_S`, `RAG_PARSE_TIMEOUT_S`, `ZEUS_APP_ID`, `ZEUS_APP_SECRET`,
`ZEUS_INDEX`, `ZEUS_KB_LIMIT`, `ZEUS_MAX_K`, `ZEUS_RECALL_MIN_SCORE`,
`ZEUS_RERANK_MIN_SCORE`, `ZEUS_MAX_CONCURRENCY`, `ZEUS_UPLOAD_TIMEOUT_S`,
`PARSE_FALLBACK`, `LLM_PROVIDER`, `LLM_MODEL_NAME`.

LLM selection: `LLM_MODEL_NAME` (default `qwen3.8-27b`) resolves
`config/llm/<model>.yaml` and is sent as the request model. `LLM_PROVIDER` picks
the yaml section and the provider implementation — `openai`
(OpenAI-compatible, default) or `pingan` (intranet gateway with signed headers,
section keys `appKey`, `appSecret`, `rsaPrivateKey`, `openApiCredential`,
`sceneId`, `requestId`).

## Smoke test

With MongoDB and the service running, exercise the real HTTP API end to end:

```bash
./scripts/smoke_test.sh health       # /healthz + route prefix
./scripts/smoke_test.sh library      # upload upload_materials/internal, build index
./scripts/smoke_test.sh regulation   # upload upload_materials/external, run chain
./scripts/smoke_test.sh push         # POST /regulations/push + docId result pull
./scripts/smoke_test.sh              # all of the above

# one command per endpoint, for step-by-step experiments
uv run python scripts/api_lab.py health
uv run python scripts/api_lab.py chain upload_materials/external/some.docx
```

`scripts/README.md` also has an intranet migration checklist.

See `scripts/README.md` for environment overrides and supported file types.

## Development

```bash
uv run --extra dev python -m pytest tests/ -q    # 154 tests, fully offline
```

The e2e test drives the real chain (upload → applicability → split → judge →
episodes → review HTML) against a scripted LLM and an in-memory fake
RAG service — no network, no MongoDB.

Notes: run uvicorn with 1 worker (in-process queue); scanning PDFs have no local
OCR — rely on the remote parse or mark the clause for manual review; the Docker
base image must be Python ≥ 3.12 (`requires-python`).
