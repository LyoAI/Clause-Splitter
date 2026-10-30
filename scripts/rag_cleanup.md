# RAG 语料清理 runbook(ChillRAG / Zeus / 本服务侧)

测试期会反复上传同一批文档、重复跑端到端,语料与镜像会越积越多。三类残留
要分别清:**远端 RAG 的已入库文件**、**本服务 Mongo 的镜像与测试法规**、
**拆条远端解析的暂存**。按需取用下面三节,命令均可整段复制。

约定:仓库根目录执行;`BASE=http://127.0.0.1:18998/api/v1/clause_splitter`;
ChillRAG 目录以 `$CHILLRAG` 表示(默认 `/Users/mac/Workspace/PingAn/ChillRAG`,
内网按实际路径替换)。

```bash
BASE=http://127.0.0.1:18998/api/v1/clause_splitter
CHILLRAG=/Users/mac/Workspace/PingAn/ChillRAG
```

---

## 0. 先看现状(决定要删什么)

```bash
# 本服务视角:语料文档数/切片数 + 镜像列表(remote_doc_id 就是远端文档 id)
curl -sS "$BASE/library/index"  | python3 -m json.tool --no-ensure-ascii
curl -sS "$BASE/library/documents" | python3 -m json.tool --no-ensure-ascii
```

```bash
# ChillRAG 侧:直接看源文件目录(语料库 + 解析暂存两个 folder)
ls -la "$CHILLRAG/materials/clause_splitter_internal/"
ls -la "$CHILLRAG/materials/clause_splitter_parse/"
```

---

## 1. ChillRAG:删源文件 + rebuild

ChillRAG **没有删除接口**(设计如此,解析暂存只进不出),且增量 ingest 按
"文件名+大小+mtime"跳过——同名同内容重复上传会覆盖,不堆积;**不同文件名的
重复内容才会堆积**。清理 = 删源文件后重建派生缓存:

```bash
# ① 停服务(rebuild 会整目录重解析,避免和运行中的服务抢 embedding 模型)
pkill -f 'uvicorn app.main:create_app'    # 多实例时逐个 kill,见 lsof -iTCP:9022 -sTCP:LISTEN

# ② 删掉不要的源文件(整个语料重来就删目录)
rm "$CHILLRAG/materials/clause_splitter_internal/某测试文档.docx"
# rm -rf "$CHILLRAG/materials/clause_splitter_internal"     # 推倒重来
# rm -rf "$CHILLRAG/materials/clause_splitter_parse"        # 解析暂存一并清

# ③ 重建派生缓存(重新解析/切块/embed,文档多时耗时数分钟)
cd "$CHILLRAG" && .venv/bin/python -m app.ingest clause_splitter_internal --rebuild

# ④ 重启服务(保持 --workers 1)
cd "$CHILLRAG" && .venv/bin/python -m uvicorn app.main:create_app --factory \
    --workers 1 --host 0.0.0.0 --port 9022
```

> rebuild 后 file_id(=文件名哈希)对保留文件不变,老 chunk_id 稳定;
> 已删文档的 chunk_id 消失。若 `.cache` 损坏导致状态异常,同样走这节。

## 2. Zeus:有删除接口,按 org_data_id 批量清

远端删除由本服务代发,**不需要 mongosh,也不用手工记 org_data_id**:

```bash
# ① 列出已入库文档(id / filename / kind / remote_doc_id / remote_state)
.venv/bin/python -I scripts/api_lab.py lib-list

# ② 单个删(远端文档 + 镜像行一起清)
.venv/bin/python -I scripts/api_lab.py lib-delete <doc_id>

# ③ 一键清空(先确认 ① 里都是测试文档)
.venv/bin/python - <<'PY'
import os, sys
sys.path.insert(0, "scripts")
from api_lab import Client, DEFAULT_BASE, _env_value
c = Client(os.environ.get("BASE_URL", DEFAULT_BASE), _env_value("API_KEY"))
for doc in c.data("GET", "/library/documents")["items"]:
    print("delete", doc["id"], doc["filename"], doc["remote_doc_id"])
    c.call("DELETE", f"/library/documents/{doc['id']}", quiet=True)
PY
```

> Zeus 删除是即时接口,但索引后台异步;删完立刻搜仍可能命中,属正常。
> 注意只删测试标签(`internal-regulation`/`attach-regulation`)下的文档,
> 共享知识库里他人的文档不要动——`lib-list` 的 `kind` 列标明来源
> (`library` 直传 / `internal` 内规 / `attach` 附件),按它挑。
> 远端删除失败(接口不支持/404)时只清镜像行,日志会写明。

## 3. 本服务侧:镜像行 + 测试法规(不影响 RAG)

```bash
# 找测试法规 id(doc_kind 列:external / internal / attach)
.venv/bin/python -I scripts/api_lab.py list --limit 50

# 逐个删(连带条款/任务/落盘文件;内规/附件同时删远端文档与镜像行)
.venv/bin/python -I scripts/api_lab.py delete <regulation_id>

# 一把清掉所有法规(慎用;逐条走 DELETE,行为同单删)
.venv/bin/python - <<'PY'
import os, sys
sys.path.insert(0, "scripts")
from api_lab import Client, DEFAULT_BASE, _env_value
c = Client(os.environ.get("BASE_URL", DEFAULT_BASE), _env_value("API_KEY"))
for reg in c.data("GET", "/regulations?limit=200")["items"]:
    print("delete", reg["id"], reg["doc_kind"], reg["title"])
    c.call("DELETE", f"/regulations/{reg['id']}", quiet=True)
PY

rm -rf data/regulations/* data/internal/docs/*                # 落盘产物
```

> 镜像行随 `lib-delete` 一并清,无需直连 Mongo。排查时确需直连的话:内网默认
> `ng4t5e57-dds.dbstg.paic.com.cn:14444`、库 `temple_saas`,密码走 CyberArk;
> 脚本里先 `load_dotenv(".env")` 再读 `config_mongo`,否则会按内网地址连而本地
> 没有凭据(表现为 SSL 报错)。

> 直连 Mongo 清理后不需要重启服务;`/library/index` 会显示 0 文档。
> 若保留了 ChillRAG 语料而只清镜像,记得重新 `POST /library/index` 对账。

> **只删 `data/regulations/*` 而不清 Mongo 会留下孤儿登记行** —— 现在不再卡死:
> 同内容再上传/push 会自动补写源文件(日志 `源文件缺失,已用本次上传的内容重建`),
> 内规库镜像行同样会自愈。但旧的拆条/分析结果不会自动重跑,要重跑用
> `api_lab.py run <reg_id>` 或 `api_lab.py rerun <file>`。想彻底干净就按本节一起清。

## 4. 清完自检

```bash
curl -sS "$BASE/library/index"     # doc_count/chunk_count 符合预期
curl -sS "$BASE/library/documents" # 镜像与 RAG 实际内容一致
python3 scripts/zeus_probe.py search --query "数据安全"   # zeus:确认测试文档不再命中
```
