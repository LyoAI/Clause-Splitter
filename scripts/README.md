# 测试脚本

服务启动、MongoDB 可连通后，在仓库根目录执行：

```bash
./scripts/smoke_test.sh          # 一键冒烟：健康检查 → 内规建索引 → 外规整链 → push/结果
uv run python scripts/api_lab.py # 逐接口手工实验，见下文
```

`smoke_test.sh` 适合回归；`api_lab.py` 适合边看服务日志边逐个接口试参数。

## 冒烟脚本命令

```bash
./scripts/smoke_test.sh health          # /healthz + OpenAPI 路由前缀
./scripts/smoke_test.sh library         # 上传 upload_materials/internal/*，构建索引
./scripts/smoke_test.sh regulation      # 上传 external 下第一个文件并跑整链
./scripts/smoke_test.sh regulation ./upload_materials/external/某文件.docx
./scripts/smoke_test.sh push            # optType=0，随后拉 docId 结果
./scripts/smoke_test.sh push 1          # optType=1，auto 模式下进入 scheduled
./scripts/smoke_test.sh all             # 全部（默认）
./scripts/watch_job.sh <job_id>         # 另开终端实时轮询一个任务
```

每个脚本失败时会打印接口原始响应并退出。

## 可覆盖的环境变量

```bash
HOST=127.0.0.1 \
SERVICE_PORT=18998 \
API_KEY=你的key \
POLL_TIMEOUT=1800 \
POLL_INTERVAL=2 \
MATERIALS_DIR=./upload_materials \
./scripts/smoke_test.sh
```

- `API_KEY` 未设置时会尝试从根目录 `.env` 读取；仍为空则不发 `X-API-Key`（本地无鉴权）。
- `POLL_TIMEOUT` 是单次等待任务/结果的上限，单位秒；LLM 分析较慢时可调大。
- `push` 默认 `optType=0`；也支持 `push 1` 测自动推送的 `scheduled` 分支。
- 脚本里的 push 会走真实回调配置：如果 `.env` 配了 `BACKEND_CALLBACK_URL` 或 ESG，任务完成后会真的发回调。

## 接口实验脚本 `api_lab.py`

一个子命令对应一个接口，直接打印请求方法与原始 JSON，无需另学 curl。默认地址
`http://127.0.0.1:18998/api/v1/clause_splitter`，可用 `--base` 指到任意环境。

```bash
export API_KEY=...          # 服务配了 API_KEY 时才需要
B=http://127.0.0.1:18998/api/v1/clause_splitter

uv run python scripts/api_lab.py --base "$B" health
uv run python scripts/api_lab.py --base "$B" lib-upload upload_materials/internal/某制度.docx
uv run python scripts/api_lab.py --base "$B" lib-index --wait
uv run python scripts/api_lab.py --base "$B" lib-status
uv run python scripts/api_lab.py --base "$B" upload upload_materials/external/某办法.docx
uv run python scripts/api_lab.py --base "$B" run <reg_id> --interval 5
uv run python scripts/api_lab.py --base "$B" clauses <reg_id> --status 缺失
uv run python scripts/api_lab.py --base "$B" artifacts <reg_id>
uv run python scripts/api_lab.py --base "$B" artifact <reg_id> review.html --out /tmp
uv run python scripts/api_lab.py --base "$B" push --doc-id DOC-1 \
  --path upload_materials/external/某办法.docx --wait-result
uv run python scripts/api_lab.py --base "$B" result DOC-1
```

- `chain <file>` = 上传 + 跑整链 + 等待 + 打印阶段状态与产物，用于快速验证全链路。
- 单阶段：`apply` / `split` / `internalize` / `analyze`，加 `--wait` 会轮询到结束；
  `run` 默认等待，可用 `--no-wait` 只入队。
- `wait <job_id>` 可单独接管已有任务；`job` / `jobs` 查任务，`--state`、`--job-type` 过滤。
- `push` 支持 `--path`（本地文件，走 `doclobsUrl`）、`--url`（http 下载）、`--content`（纯文本）。
  `--doc-type` 对应后端的 `docType`（1 外规 / 2 内规 / 3 附件）；不传则模拟缺失，
  用于观察按外规兜底的 WARNING 日志。脚本会回查 `/regulations/{id}` 打印
  `docType → kind → 链路`（推送响应本身是与后端约定的固定字段，不为此新增）。
  内规/附件应看到 `链路=内规/附件(入库+拆条)`。
- 验证内规/附件：`push --doc-type 2` 之后，`lib-list` 应出现 `kind=internal` 的镜像行，
  `result` 的 `applicable` 应为 `null`、`data[]` 有条款。
- 失败时脚本退出码非 0；HTTP 非 2xx 或信封 `status=error` 都会直接打印响应后退出。

`python scripts/api_lab.py <命令> -h` 查看该接口的全部参数。

## Zeus 探针 `zeus_probe.py`

服务日志把 Zeus 失败压成一句「Zeus 业务错误: error」时，用这个脚本直连 Zeus，
它会打印完整请求与响应体。不依赖服务、Mongo、LLM，只需要 `RAG_API_BASE` /
`ZEUS_APP_ID` / `ZEUS_APP_SECRET` / `ZEUS_INDEX`（读 `.env`，也可用参数覆盖）。

```bash
uv run python scripts/zeus_probe.py ping                       # 连通+鉴权（一条平凡检索）
uv run python scripts/zeus_probe.py upload --file 条例.docx     # 只测上传
uv run python scripts/zeus_probe.py all --file 条例.docx \
  --label internal-regulation --wait 180                        # 上传→提交→轮询可检索
uv run python scripts/zeus_probe.py all --file 条例.docx --dry-run   # 只看要发什么
```

`upload` / `all` 的 `--part-ctype` 用来对齐 Postman：

| 值 | multipart part 的 Content-Type |
|---|---|
| `auto`（默认） | 按后缀推，同 Postman；**生产代码现在的行为** |
| `octet` | 固定 `application/octet-stream`；改造前的行为，Zeus 会回 `msg=error` |

其它子命令：`submit --url --filename` 手工提交已上传的文件、`search --query` 检索、
`delete --org-id` 删除。每个请求都会打印 request-id（Zeus 报错时带上它找 RAG 团队）。

## 调度模式：实时 vs 定时

相关配置在 `.env`：

```ini
CHAIN_DISPATCH=auto          # auto | immediate | cron
CHAIN_CRON=0 20 * * mon-sat  # 5 段 crontab，服务端本地时区
```

| CHAIN_DISPATCH | `optType=0`（手动） | `optType=1`（自动） |
|---|---|---|
| `immediate` | 立即执行 | 立即执行 |
| `auto` | 立即执行 | `scheduled`，等 cron 放行 |
| `cron` | `scheduled`，等 cron 放行 | `scheduled`，等 cron 放行 |

要点：

- 改 `.env` 后必须重启服务，配置在进程启动时读取。
- `/regulations/{id}/run` 和单阶段接口不受 `CHAIN_DISPATCH` 影响，始终立即入队；所以 `smoke_test.sh regulation` 测的是实时任务。
- push 才走调度器：`auto` + `optType=0` 是实时，`auto` + `optType=1` 或 `cron` 是定时。
- 定时任务会一直保持 `scheduled`，直到 cron 触发 `drain_scheduled`；重启服务不会丢 scheduled 任务。
- 从 `cron/auto` 切到 `immediate` 后不会启动调度器；已经处于 `scheduled` 的旧任务不会自己跑，需要切回 cron 放行或手工处理。

### 实时任务测试

```ini
CHAIN_DISPATCH=auto
```

重启后：

```bash
./scripts/smoke_test.sh regulation   # 上传+整链，始终立即执行
./scripts/smoke_test.sh push 0       # auto 模式下 optType=0 立即执行
```

### 定时任务测试

把 cron 临时改成每分钟，方便验证：

```ini
CHAIN_DISPATCH=auto
CHAIN_CRON=*/1 * * * *
```

重启后确认日志出现：

```text
chain dispatcher cron armed: */1 * * * *
```

然后：

```bash
./scripts/smoke_test.sh push 1
```

脚本会显示 `dispatch=scheduled` 并给出 `job_id`，不会干等 cron。另开终端跟踪：

```bash
./scripts/watch_job.sh <job_id>
```

cron 触发后日志会出现 `dispatcher admitted N scheduled job(s)`，job 状态依次变为 `queued → running → done`。也可以查：

```bash
BASE=http://127.0.0.1:18998/api/v1/clause_splitter
curl -sS "$BASE/jobs?state=scheduled" | python3 -m json.tool
```

## 接口覆盖范围

`smoke_test.sh` 只覆盖主链路；其余接口用 `api_lab.py` 逐条实验。

| smoke_test 已覆盖 | 说明 |
|---|---|
| `GET /healthz` | 存活检查 |
| `GET /openapi.json` | 确认新路由前缀已挂载 |
| `POST /library/documents` | 上传内规 |
| `POST /library/index` | 建索引并等待 job |
| `GET /library/index` | 索引状态 |
| `POST /regulations` | 上传外规 |
| `POST /regulations/{id}/run` | 跑完整四阶段 |
| `GET /regulations/{id}` | 阶段状态、条款数 |
| `GET /regulations/{id}/clauses` | 条款总数（只取 limit=1） |
| `GET /regulations/{id}/artifacts` | 产物列表 |
| `GET /jobs/{id}` | 轮询任务状态 |
| `POST /regulations/push` | 后端推送，支持 `optType` |
| `GET /regulations/{docId}/result` | 按 docId 拉取结果 |

`api_lab.py` 覆盖的接口：

| 命令 | 接口 |
|---|---|
| `health` | `GET /healthz`（根路径，不带 API 前缀） |
| `list` / `get` / `delete` | `GET|DELETE /regulations[/{id}]` |
| `upload` / `chain` | `POST /regulations` |
| `apply` / `split` / `internalize` / `analyze` / `run` | 对应阶段接口 |
| `patch-applicability` | `PATCH /regulations/{id}/applicability` |
| `clauses` / `clause` | `GET /regulations/{id}/clauses[/{clause_id}]` |
| `jobs` / `job` / `wait` | `GET /jobs[/{id}]`、`GET /regulations/{id}/jobs` |
| `artifacts` / `artifact` | 产物列表与下载（含 `episodes/xxx.json`） |
| `lib-upload` / `lib-list` / `lib-delete` | `POST|GET|DELETE /library/documents` |
| `lib-index` / `lib-status` | `GET|POST /library/index` |
| `push` / `result` | 后端契约的两个入口 |

回调（ESG / `BACKEND_CALLBACK_URL`）没有脚本入口：配好 `.env` 后跑 `push`，观察服务日志与
回调接收端即可。

## 测试材料目录

```text
upload_materials/
├── external/     外规：待拆条、内化判定的法规
└── internal/     内规：用于检索比对的公司制度
```

`upload_materials/` 已在 `.gitignore` 中，不会被提交。脚本按文件名顺序读取目录下第一个非隐藏文件；也可以给 `regulation` 子命令直接传路径。

## 支持的文件类型

### 外规（`POST /regulations`、`POST /regulations/push`）

| 后缀 | 说明 |
|---|---|
| `.pdf` `.docx` `.pptx` `.xlsx` `.png` `.jpg` `.md` | 优先走 RAG provider 解析（上传→轮询→切片文本）；远端不可用、报错或 provider 不返回文本（如 `zeus`）时本地回退 |
| `.txt` | 本地按行合成 blocks，适合简单测试 |
| `.doc` `.xls` `.jpeg` | 本地抽取：`.doc` 用 `antiword`（没有时只能识别误命名的 docx），`.xls` 用 openpyxl |

远端失败回退由 `PARSE_FALLBACK=local` 控制；`structure.json` 的 `parse_source` 记录本次走的是
`remote` 还是 `local`。扫描件本地无 OCR，远端是否支持取决于 RAG 服务。
`RAG_PROVIDER=zeus` 时接口不返回解析文本，外规固定走本地。

push 接口按文件魔数识别 `PDF / DOCX / DOC / UTF-8 文本`，不依赖文件名后缀。

### 内规库（`POST /library/documents`）

| 后缀 | 说明 |
|---|---|
| `.pdf` `.docx` `.pptx` `.xlsx` `.png` `.jpg` `.jpeg` `.md` `.markdown` | 交给 RAG provider 入库，由其解析、分块、索引 |

本仓只保留原文镜像与 Mongo 镜像行；实际解析/分块/索引都在 RAG 服务侧。
`GET /library/index` 的 `rag.available=false` 表示 `RAG_API_BASE` 或 provider 凭据没配齐。

上传大小上限由 `MAX_UPLOAD_MB` 控制，默认 200 MB。

## 迁移内网后的联调步骤

1. 在 `.env`（或容器环境变量）里配置 RAG 与 LLM，重启服务：

   ```ini
   RAG_PROVIDER=chillrag            # 或 zeus（内网知识库）
   RAG_API_BASE=http://内网RAG地址
   RAG_API_USER=...                 # chillrag 登录用
   RAG_API_PASSWORD=...
   RAG_CORPUS=clause_splitter_internal
   RAG_PARSE_CORPUS=clause_splitter_parse
   ```

   `zeus` 改为填写凭据（详见 `assets/rag-service-contract.md`）：

   ```ini
   RAG_PROVIDER=zeus
   RAG_API_BASE=http://zeus 域名
   ZEUS_APP_ID=...
   ZEUS_APP_SECRET=...
   ZEUS_INDEX=...
   ZEUS_CREATED_BY=<UM 号>
   ZEUS_LABEL_INTERNAL=internal-regulation
   ZEUS_LABEL_ATTACH=attach-regulation
   ZEUS_SEARCH_LABELS=            # 空/all = 整个知识库;internal-regulation,attach-regulation = 两类
   ```

   注意：`RAG_*` / `ZEUS_*` 只在进程启动时读取；改完必须重启，不能热更。

2. 服务起来后先确认连通性与语料状态：

   ```bash
   export B=http://内网服务地址:18998/api/v1/clause_splitter
   uv run python scripts/api_lab.py --base "$B" health
   uv run python scripts/api_lab.py --base "$B" lib-status
   ```

   `lib-status` 里 `rag.available` 应为 `true`；`false` 说明 `RAG_API_BASE` 没生效或 provider 初始化失败。

3. 内规入库并建索引（第一次会触发远端解析，耗时取决于文件大小）：

   ```bash
   uv run python scripts/api_lab.py --base "$B" lib-upload upload_materials/internal/某制度.docx
   uv run python scripts/api_lab.py --base "$B" lib-index --wait
   uv run python scripts/api_lab.py --base "$B" lib-status
   ```

   检查点：上传返回 `remote_state=indexed` 且 `chunk_count>0`；`lib-status` 的 `rag.version` 非空；
   重复 `lib-index` 时 version 不变（语料未变），新增文件后 version 变化。
   `zeus` 例外：没有状态/统计接口，`remote_state` 是乐观值、`chunk_count=0`（未知）、
   `rag.version` 为空；提交后要等 Zeus 后台解析/分块/embedding 完成才能检索到。

   `zeus` 的内规必须带 `internal-regulation` 标签才检索得到：入库自动打，
   但如果库里有改造前入库的旧文档，需要重新 `lib-index` 一次。附件用
   `attach-regulation`，检索默认两类都覆盖。

4. 外规解析（确认真的走远端）：

   ```bash
   uv run python scripts/api_lab.py --base "$B" upload upload_materials/external/某办法.docx
   uv run python scripts/api_lab.py --base "$B" split <reg_id> --wait
   uv run python scripts/api_lab.py --base "$B" artifact <reg_id> structure.json --out /tmp
   grep parse_source /tmp/structure.json
   ```

   `parse_source` 应为 `remote`，条款数为多条而不是整篇一段。若为 `local`，先看服务日志里
   `远端解析失败(...)` 的原因（鉴权、网络、超时或不支持的后缀）。`RAG_PROVIDER=zeus` 时
   固定为 `local`：该 provider 不返回解析文本，属预期行为。

5. 内化与覆盖分析（真实 LLM + 真实检索）：

   ```bash
   uv run python scripts/api_lab.py --base "$B" internalize <reg_id> --wait
   uv run python scripts/api_lab.py --base "$B" analyze <reg_id> --wait
   uv run python scripts/api_lab.py --base "$B" clauses <reg_id> --include-candidates
   uv run python scripts/api_lab.py --base "$B" artifacts <reg_id>
   ```

   检查点：`matched_internal_clause_ids` 里的每个 id 都来自本轮检索；`status` 只能是
   `完全覆盖 / 部分覆盖 / 缺失`；`review.html` 按制度名分组。LLM 慢时可先只跑 `split`。

6. 后端契约：

   ```bash
   uv run python scripts/api_lab.py --base "$B" push --doc-id DOC-INTRANET-1 \
     --path upload_materials/external/某办法.docx --wait-result
   uv run python scripts/api_lab.py --base "$B" result DOC-INTRANET-1
   ```

   检查点：push 返回 `job_id`、`dispatch`、`manual`；处理中 `result.code="2"`，成功 `"1"`，
   失败 `"0"`；`data` 永远是数组，`termId={docId}-{orderNum}`。配了回调时同时观察
   ESG 网关/`BACKEND_CALLBACK_URL` 是否收到同一份 payload。

7. 全链路一把跑：`uv run python scripts/api_lab.py --base "$B" chain upload_materials/external/某办法.docx`。
