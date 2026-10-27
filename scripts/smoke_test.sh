#!/usr/bin/env bash
# Smoke-test the running Clause_Splitter service.
#
# Usage:
#   ./scripts/smoke_test.sh                 # all checks
#   ./scripts/smoke_test.sh health          # liveness + new route prefix
#   ./scripts/smoke_test.sh library         # upload internal docs, build index
#   ./scripts/smoke_test.sh regulation      # upload external doc, run the chain
#   ./scripts/smoke_test.sh regulation FILE # run one specific external file
#   ./scripts/smoke_test.sh push            # backend push + docId result pull
#   ./scripts/smoke_test.sh push 1          # optType=1 (auto dispatch -> scheduled)
#
# Environment overrides:
#   HOST=127.0.0.1 SERVICE_PORT=18998 API_KEY=... POLL_TIMEOUT=1800 ./scripts/smoke_test.sh

set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_common.sh
source "$SCRIPT_DIR/_common.sh"

command -v curl >/dev/null 2>&1 || die "curl not found"

SKIPPED=0

cmd_health() {
    say "1. health & route mount"
    local health openapi
    health="$(curl -sS --max-time 10 "${BASE_URL}/healthz")" || die "cannot reach ${BASE_URL}/healthz"
    [ "$health" = "ok" ] && ok "GET /healthz -> ok" || die "GET /healthz -> ${health}"

    openapi="$(curl -sS --max-time 20 "${BASE_URL}/openapi.json")" || die "cannot read /openapi.json"
    if printf '%s' "$openapi" | grep -q '"/api/v1/clause_splitter/regulations/push"'; then
        ok "route prefix /api/v1/clause_splitter is mounted"
    else
        die "OpenAPI does not contain /api/v1/clause_splitter; old code still running?"
    fi
}

cmd_library() {
    say "2. internal library: upload + index"
    local body f resp code msg uploaded=0
    local job_id

    body="$(api GET /library/index)"
    envelope_ok "$body" "library index status"
    if [ "$(printf '%s' "$body" | json_opt data.rag.available)" = "False" ]; then
        warn "RAG_API_BASE is not configured; uploads stay pending and the index build will fail"
    fi
    ok "before: doc_count=$(printf '%s' "$body" | json_opt data.doc_count) chunk_count=$(printf '%s' "$body" | json_opt data.chunk_count)"

    for f in "$MATERIALS_DIR"/internal/*; do
        [ -f "$f" ] || continue
        case "$(basename "$f")" in .*) continue ;; esac
        printf '  upload internal: %s ... ' "$(basename "$f")"
        resp="$(curl -sS --max-time 300 -X POST "${API_BASE}/library/documents" \
            "${AUTH[@]}" -F "file=@${f}")"
        code="$(printf '%s' "$resp" | json_opt code)"
        msg="$(printf '%s' "$resp" | json_opt msg)"
        if [ "$code" != "200" ]; then
            printf '\n'
            print_json "$resp"
            die "internal upload failed: ${msg:-no message}"
        fi
        printf '%s\n' "${msg:-ok}"
        uploaded=$((uploaded + 1))
    done

    if [ "$uploaded" -eq 0 ]; then
        warn "no internal files under ${MATERIALS_DIR}/internal; analysis will have no internal clauses"
        SKIPPED=1
        return 0
    fi

    body="$(api POST /library/index)"
    envelope_ok "$body" "library index trigger"
    job_id="$(printf '%s' "$body" | json_opt data.job_id)"
    [ -n "$job_id" ] || die "library index trigger returned no job_id"
    ok "index job queued: ${job_id}"
    wait_job "$job_id"

    body="$(api GET /library/index)"
    envelope_ok "$body" "library index status"
    ok "after: doc_count=$(printf '%s' "$body" | json_opt data.doc_count) chunk_count=$(printf '%s' "$body" | json_opt data.chunk_count)"
}

cmd_regulation() {
    say "3. external regulation: upload -> run chain -> results"
    local file="${1:-}" body resp reg_id job_id title duplicate
    local lib
    lib="$(api GET /library/index)"
    if [ "$(printf '%s' "$lib" | json_opt data.rag.available)" = "False" ]; then
        warn "RAG_API_BASE is not configured; an applicable regulation will fail at analysis"
    fi
    if [ "$(printf '%s' "$lib" | json_opt data.doc_count)" = "0" ] || \
       [ -z "$(printf '%s' "$lib" | json_opt data.doc_count)" ]; then
        warn "internal library is empty; the analysis stage will not find internal clauses"
    fi

    if [ -z "$file" ]; then
        file="$(first_material "${MATERIALS_DIR}/external")" || \
            die "no external file under ${MATERIALS_DIR}/external; pass a file path explicitly"
    fi
    [ -f "$file" ] || die "external file not found: ${file}"

    title="$(basename "$file")"
    title="${title%.*}"
    printf '  upload external: %s\n' "$file"
    resp="$(curl -sS --max-time 300 -X POST "${API_BASE}/regulations" \
        "${AUTH[@]}" -F "file=@${file}" -F "title=${title}")"
    envelope_ok "$resp" "external upload"
    reg_id="$(printf '%s' "$resp" | json_opt data.id)"
    duplicate="$(printf '%s' "$resp" | json_opt data.duplicate)"
    [ -n "$reg_id" ] || die "upload returned no regulation id"
    ok "regulation_id=${reg_id} duplicate=${duplicate:-false}"

    body="$(api POST "/regulations/${reg_id}/run")"
    envelope_ok "$body" "chain trigger"
    job_id="$(printf '%s' "$body" | json_opt data.id)"
    [ -n "$job_id" ] || die "chain trigger returned no job id"
    ok "chain job queued: ${job_id}"
    wait_job "$job_id"
    show_regulation "$reg_id"
}

cmd_push() {
    local opt_type="${1:-0}"
    case "$opt_type" in
        0|1) ;;
        *) die "optType must be 0 (manual) or 1 (auto), got: ${opt_type}" ;;
    esac
    say "4. backend push -> docId result (optType=${opt_type})"
    local stamp doc_id payload resp dispatch job_id body code clauses
    stamp="$(date +%Y%m%d%H%M%S)"
    doc_id="smoke-push-${stamp}"

    payload="$(cat <<JSON
{
  "args": {
    "docId": "${doc_id}",
    "taskId": "task-${stamp}",
    "uid": "smoke",
    "docType": "2",
    "optType": ${opt_type},
    "extraParams": {"source": "smoke-test", "run": "${stamp}"},
    "docContent": "第一条 为了规范数据安全管理，制定本办法（冒烟测试 ${stamp}）。\n第二条 数据处理者应当建立数据安全管理制度，并定期开展风险评估。\n第三条 本办法自发布之日起施行。"
  }
}
JSON
)"
    resp="$(api POST /regulations/push "$payload")"
    envelope_ok "$resp" "backend push"
    dispatch="$(printf '%s' "$resp" | json_opt data.dispatch)"
    job_id="$(printf '%s' "$resp" | json_opt data.job_id)"
    ok "push accepted: docId=${doc_id} dispatch=${dispatch:-?} job=${job_id:-?}"

    if [ "$dispatch" = "scheduled" ]; then
        warn "push is scheduled (CHAIN_DISPATCH=cron, or optType=1 under auto)."
        warn "it waits for CHAIN_CRON; follow it with: ./scripts/watch_job.sh ${job_id:-<job_id>}"
        warn "for an immediate test use optType=0 with CHAIN_DISPATCH=auto, or CHAIN_DISPATCH=immediate."
        SKIPPED=1
        return 0
    fi

    wait_result "$doc_id"

    body="$(api GET "/regulations/${doc_id}/result")"
    # Flat backend payload: no envelope, no extraParams echo.
    code="$(printf '%s' "$body" | json_opt code)"
    if [ "$code" != "1" ]; then
        print_json "$body"
        die "docId result not successful (code=${code:-none})"
    fi
    if printf '%s' "$body" | grep -q '"extraParams"'; then
        die "result payload must not echo extraParams"
    fi
    clauses="$(printf '%s' "$body" | py -c '
import json, sys
data = json.load(sys.stdin).get("data")
print(len(data) if isinstance(data, list) else "not-a-list")
' 2>/dev/null || true)"
    ok "flat result: docId=${doc_id} clauses=${clauses:-?}"
    printf '\n  result payload:\n'
    print_json "$body" | sed 's/^/    /'
}

cmd_all() {
    cmd_health
    cmd_library
    cmd_regulation
    cmd_push 0
    if [ "$SKIPPED" -eq 0 ]; then
        say "all smoke checks passed"
    else
        say "smoke checks finished, but some steps were skipped (see WARN above)"
    fi
}

usage() {
    sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
    printf '\nCommands: health | library | regulation [FILE] | push [0|1] | all\n'
}

case "${1:-all}" in
    health)     cmd_health ;;
    library)    cmd_library ;;
    regulation) shift; cmd_regulation "${1:-}" ;;
    push)       shift; cmd_push "${1:-0}" ;;
    all)        cmd_all ;;
    -h|--help|help) usage ;;
    *)          usage; exit 2 ;;
esac
