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

```bash
# 单个删(凭入库时返回的 org_data_id)
python3 scripts/zeus_probe.py delete --org-id <org_data_id>
```

测试期批量清理——镜像表 `internal_docs` 里存着每个已入库文档的
`remote_doc_id`(即 Zeus 的 org_data_id),取出来逐个删:

```bash
# ① 列出待删项(内网按实际 Mongo 地址/库名替换)
mongosh --quiet 127.0.0.1:27017/clause_splitter --eval \
  'db.internal_docs.find({},{filename:1,remote_doc_id:1}).forEach(
     d => print(d.remote_doc_id, d.filename))'

# ② 一键全部删除(先确认 ① 的列表都是测试文档再跑)
mongosh --quiet 127.0.0.1:27017/clause_splitter --eval \
  'db.internal_docs.find({},{remote_doc_id:1,_id:0}).forEach(
     d => print(d.remote_doc_id))' | grep -v '^$' | \
  while read -r rid; do
    echo "== delete $rid"
    python3 scripts/zeus_probe.py delete --org-id "$rid"
  done
```

> Zeus 删除是即时接口,但索引后台异步;删完立刻搜仍可能命中,属正常。
> 注意只删测试标签(`internal-regulation`/`attach-regulation`)下的文档,
> 共享知识库里他人的文档不要动。没有 mongosh 时查列表可用:
> `.venv/bin/python -c "from pymongo import MongoClient; [print(r.get('remote_doc_id'), r.get('filename')) for r in MongoClient('127.0.0.1:27017').clause_splitter.internal_docs.find()]"`

## 3. 本服务侧:镜像行 + 测试法规(不影响 RAG)

```bash
# 逐个删法规(会连带条款/任务/落盘文件;内规/附件同时清掉 internal_docs 镜像行)
.venv/bin/python -I scripts/api_lab.py list --limit 50          # 找测试法规 id
.venv/bin/python -I scripts/api_lab.py delete <regulation_id>

# 一把清掉所有测试残留(慎用;mongosh 直连,MONGO_DBNAME 默认 clause_splitter)
mongosh --quiet 127.0.0.1:27017/clause_splitter --eval '
  db.internal_docs.deleteMany({});       // 语料镜像
  db.clauses.deleteMany({});             // 拆条/分析结果
  db.jobs.deleteMany({});                // 任务记录
  db.regulations.deleteMany({});         // 法规(含 push 记录)
'
rm -rf data/regulations/* data/internal/docs/*                   // 落盘产物
```

> 直连 Mongo 清理后不需要重启服务;`/library/index` 会显示 0 文档。
> 若保留了 ChillRAG 语料而只清镜像,记得重新 `POST /library/index` 对账。

## 4. 清完自检

```bash
curl -sS "$BASE/library/index"     # doc_count/chunk_count 符合预期
curl -sS "$BASE/library/documents" # 镜像与 RAG 实际内容一致
python3 scripts/zeus_probe.py search --query "数据安全"   # zeus:确认测试文档不再命中
```
