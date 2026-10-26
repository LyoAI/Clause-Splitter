# 冒烟测试脚本

服务启动、MongoDB 可连通后，在仓库根目录执行：

```bash
./scripts/smoke_test.sh
```

默认依次执行：健康检查 → 内规上传/建索引 → 外规上传/跑整链 → 后端 push/结果拉取。

## 命令

```bash
./scripts/smoke_test.sh health          # /healthz + OpenAPI 路由前缀
./scripts/smoke_test.sh library         # 上传 upload_materials/internal/*，构建索引
./scripts/smoke_test.sh regulation      # 上传 external 下第一个文件并跑整链
./scripts/smoke_test.sh regulation ./upload_materials/external/某文件.docx
./scripts/smoke_test.sh push            # OptType=0，随后拉 docId 结果
./scripts/smoke_test.sh push 1          # OptType=1，auto 模式下进入 scheduled
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
- `push` 默认 `OptType=0`；也支持 `push 1` 测自动推送的 `scheduled` 分支。
- 脚本里的 push 会走真实回调配置：如果 `.env` 配了 `BACKEND_CALLBACK_URL` 或 ESG，任务完成后会真的发回调。

## 调度模式：实时 vs 定时

相关配置在 `.env`：

```ini
CHAIN_DISPATCH=auto          # auto | immediate | cron
CHAIN_CRON=0 20 * * mon-sat  # 5 段 crontab，服务端本地时区
```

| CHAIN_DISPATCH | `OptType=0`（手动） | `OptType=1`（自动） |
|---|---|---|
| `immediate` | 立即执行 | 立即执行 |
| `auto` | 立即执行 | `scheduled`，等 cron 放行 |
| `cron` | `scheduled`，等 cron 放行 | `scheduled`，等 cron 放行 |

要点：

- 改 `.env` 后必须重启服务，配置在进程启动时读取。
- `/regulations/{id}/run` 和单阶段接口不受 `CHAIN_DISPATCH` 影响，始终立即入队；所以 `smoke_test.sh regulation` 测的是实时任务。
- push 才走调度器：`auto` + `OptType=0` 是实时，`auto` + `OptType=1` 或 `cron` 是定时。
- 定时任务会一直保持 `scheduled`，直到 cron 触发 `drain_scheduled`；重启服务不会丢 scheduled 任务。
- 从 `cron/auto` 切到 `immediate` 后不会启动调度器；已经处于 `scheduled` 的旧任务不会自己跑，需要切回 cron 放行或手工处理。

### 实时任务测试

```ini
CHAIN_DISPATCH=auto
```

重启后：

```bash
./scripts/smoke_test.sh regulation   # 上传+整链，始终立即执行
./scripts/smoke_test.sh push 0       # auto 模式下 OptType=0 立即执行
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

当前脚本覆盖主链路，不是全部接口：

| 已覆盖 | 说明 |
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
| `POST /regulations/push` | 后端推送，支持 `OptType` |
| `GET /regulations/{docId}/result` | 按 docId 拉结果 + `extraParams` 回显 |

以下接口目前没有自动覆盖，可用 `/docs` 或 curl 手工验证：

- `GET /regulations`、`DELETE /regulations/{id}`
- `PATCH /regulations/{id}/applicability`
- 单阶段执行：`POST /regulations/{id}/applicability|split|internalization|analysis`
- `GET /regulations/{id}/clauses/{clause_id}`
- `GET /regulations/{id}/jobs`、`GET /jobs`
- `GET /regulations/{id}/artifacts/{name}` 文件下载
- `GET /library/documents`、`DELETE /library/documents/{id}`
- ESG 回调 / `BACKEND_CALLBACK_URL` 回调
- push 的 `doclobsUrl` 下载路径（当前只测 `docContent`）

示例：

```bash
BASE=http://127.0.0.1:18998/api/v1/clause_splitter

curl -sS "$BASE/regulations?limit=5" | python3 -m json.tool
curl -sS "$BASE/regulations/<reg_id>/jobs" | python3 -m json.tool
curl -sS "$BASE/library/documents" | python3 -m json.tool
curl -sS -X PATCH "$BASE/regulations/<reg_id>/applicability" \
  -H 'Content-Type: application/json' \
  -d '{"applicable": true, "reason": "人工确认适用"}'
```

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
| `.pdf` | 推荐，MinerU 解析 |
| `.docx` | 推荐，MinerU 解析 |
| `.doc` | 可上传；优先用系统 `antiword` 抽取，没有 antiword 时只能识别误命名的 docx；建议转 `.docx`/`.pdf` |
| `.txt` | 按行合成 blocks，适合简单测试 |
| `.md` | Markdown 解析 |

push 接口还会按文件魔数识别 `PDF / DOCX / DOC / UTF-8 文本`，不依赖文件名后缀。

### 内规库（`POST /library/documents`）

| 后缀 | 说明 |
|---|---|
| `.pdf` `.docx` `.pptx` | MinerU 解析 |
| `.xlsx` | 多制度 Excel，按 sheet 拆成多篇内规 |
| `.xls` | 可上传，但索引阶段不支持，会提示转 `.xlsx` |
| `.png` `.jpg` `.jpeg` | 图片，MinerU 解析 |
| `.txt` `.md` | 文本解析 |

外部法规不支持图片、PPT、Excel；这些只用于内规库。

上传大小上限由 `MAX_UPLOAD_MB` 控制，默认 200 MB。
