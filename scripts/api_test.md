# 接口功能测试 runbook

逐组验证各接口功能。命令均可整段复制;前置:服务已启动
(`.venv/bin/python serving.py`,默认 18998),MongoDB 与 RAG 后端可达。

```bash
BASE=http://127.0.0.1:18998/api/v1/clause_splitter
```

## 0. 一键冒烟(推荐先跑)

```bash
./scripts/smoke_test.sh health      # 存活 + 路由前缀挂载
./scripts/smoke_test.sh library     # 内规上传 + 建索引
./scripts/smoke_test.sh regulation  # 外规上传 + 四阶段链 + 结果
./scripts/smoke_test.sh push        # 后端 push + docId 结果
./scripts/smoke_test.sh             # 以上全部
```

## 1. 健康与路由

```bash
curl -sS "http://127.0.0.1:18998/healthz"        # 预期: ok
curl -sS "$BASE/openapi.json" | python3 -c '
import json,sys; paths=json.load(sys.stdin)["paths"]
assert "/api/v1/clause_splitter/regulations/push" in paths, "push 路由未挂载"
print("routes ok:", len(paths), "paths")'
```

## 2. 内规库(library)

```bash
F=upload_materials/internal/网络数据安全管理条例.docx
# 上传(重复上传同文件会命中 sha256,提示 duplicate)
.venv/bin/python -I scripts/api_lab.py lib-upload "$F"    # 预期: state=indexed, chunks>0
.venv/bin/python -I scripts/api_lab.py lib-list           # 预期: 列表含该文档
.venv/bin/python -I scripts/api_lab.py lib-index --wait   # 重建索引,预期 job done
.venv/bin/python -I scripts/api_lab.py lib-status         # 预期: doc/chunk 计数>0, rag.available=true
.venv/bin/python -I scripts/api_lab.py lib-delete <doc_id>  # 删除(镜像+远端)
```

> ChillRAG 后端注意:chunks 是远端切块数,**无标题样式的 docx 整篇 1 片属预期**;
> 想要条级粒度,文档需带标题层级。

## 3. 法规与四阶段(手动逐阶段)

```bash
# 上传外规(再传同文件 → duplicate=true 且复用已有记录)
.venv/bin/python -I scripts/api_lab.py upload upload_materials/external/网络数据安全风险评估办法.docx
REG=<上一步输出的 regulation_id>

.venv/bin/python -I scripts/api_lab.py apply  "$REG" --wait   # 阶段1 适用性
.venv/bin/python -I scripts/api_lab.py split  "$REG" --wait   # 阶段2 拆条(判"不适用"需 --force)
.venv/bin/python -I scripts/api_lab.py internalize "$REG" --wait  # 阶段3 内化判定
.venv/bin/python -I scripts/api_lab.py analyze "$REG" --wait  # 阶段4 比对分析(最久)

# 一步全链(等价 1→4)
.venv/bin/python -I scripts/api_lab.py run "$REG"

# 同内容强制重跑(命中 sha256 → 删旧记录 → 重新上传 → 全链;id 每次都是新的)
.venv/bin/python -I scripts/api_lab.py rerun upload_materials/external/网络数据安全风险评估办法.docx

# 结果检查
.venv/bin/python -I scripts/api_lab.py get "$REG"        # 各阶段 state=done
.venv/bin/python -I scripts/api_lab.py clauses "$REG" --include-candidates
.venv/bin/python -I scripts/api_lab.py clauses "$REG" --status 部分覆盖   # 覆盖分布抽查
.venv/bin/python -I scripts/api_lab.py artifacts "$REG"
.venv/bin/python -I scripts/api_lab.py artifact "$REG" review.html --out /tmp
# 预期: /tmp/review.html 可直接打开,覆盖状态含"部分覆盖"或"完全覆盖"
```

> 阶段前置 409 属正常(未跑上一阶段);重跑某阶段会自动重置下游。
> 重跑会复用同一条记录(id 不变、结果覆盖);要全新记录用上面的 `rerun`。
> 日志里出现 `answer JSON unparseable` = 有条款判定因截断降级缺失(已知形态),
> 若大面积出现先查 LLM 通道。
> 手动删过 `data/regulations/*` 也没关系:同内容再上传会自动补写源文件
> (日志 `源文件缺失,已用本次上传的内容重建`),但旧结果仍要重跑才会更新。

## 4. 后端 push(docType 分流)——端到端主场景

```bash
STAMP=$(date +%Y%m%d%H%M%S)

# 内规(docType=2):入库+拆条,不跑适用性/内化/分析
.venv/bin/python -I scripts/api_lab.py push --doc-id "T-INT-$STAMP" \
  --path upload_materials/internal/网络数据安全管理条例.docx \
  --doc-type 2 --task-id "T-INT-$STAMP" --wait-result
# 预期: code=1, applicable=null, data[]=拆条条款, requireType=""(内规跳过判定)

# 外规(docType=1):四阶段全链(optType=0 手动 → 立即执行)
.venv/bin/python -I scripts/api_lab.py push --doc-id "T-EXT-$STAMP" \
  --path upload_materials/external/网络数据安全风险评估办法.docx \
  --doc-type 1 --task-id "T-EXT-$STAMP" --wait-result --timeout 1800
# 预期: code=1, applicable=true/false, data[].requireType ∈ {义务性要求,鼓励性要求,""},
#       需内化条款 coverageStatus ∈ {完全覆盖,部分覆盖,缺失}

# 附件(docType=3):同内规链路,标签不同
.venv/bin/python -I scripts/api_lab.py push --doc-id "T-ATT-$STAMP" \
  --path upload_materials/internal/网络数据安全管理条例.docx \
  --doc-type 3 --task-id "T-ATT-$STAMP" --wait-result

# 结果轮询(不 --wait-result 时手工拉;内部排查用,不对后端开放)
.venv/bin/python -I scripts/api_lab.py result "T-EXT-$STAMP"

# 缺 docType → 按外规处理(日志有 WARNING);重复内容 → duplicate=true 复用记录
```

> 回调:设了 ESG 或 BACKEND_CALLBACK_URL 时任务完成自动回调;本地不配则无。
> optType/docType 均大小写不敏感;报文字段以 assets/外规内化服务接口文档.docx 为准。

## 5. 任务(jobs)

```bash
.venv/bin/python -I scripts/api_lab.py jobs --state running    # 运行中
.venv/bin/python -I scripts/api_lab.py job <job_id>            # 单查(progress/step/error)
.venv/bin/python -I scripts/api_lab.py wait <job_id> --timeout 1800
```

## 6. 删除与清理

```bash
.venv/bin/python -I scripts/api_lab.py list --limit 50
.venv/bin/python -I scripts/api_lab.py delete <regulation_id>
# 预期: 有活跃任务时 409;内规/附件连带清 internal_docs 镜像
# 语料/镜像的批量清理见 scripts/rag_cleanup.md
```

## 验收清单

- [ ] health ok,路由前缀挂载
- [ ] 内规上传入库 chunks>0,索引可重建
- [ ] 外规四阶段全部 done,复核 HTML 可打开
- [ ] 覆盖状态不是清一色"缺失"(否则查 LLM 通道/RAG 语料/解析告警日志)
- [ ] push docType=1/2/3 分流正确,requireType 按口径输出
- [ ] 结果报文与接口文档字段一致
