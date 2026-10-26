# Shared helpers for scripts/*.sh. Source this file; do not execute it directly.

COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$COMMON_DIR/.." && pwd)"
cd "$ROOT_DIR" || exit 1

HOST="${HOST:-127.0.0.1}"
PORT="${SERVICE_PORT:-18998}"
BASE_URL="${BASE_URL:-http://${HOST}:${PORT}}"
API_BASE="${API_BASE:-${BASE_URL}/api/v1/clause_splitter}"
MATERIALS_DIR="${MATERIALS_DIR:-${ROOT_DIR}/upload_materials}"
POLL_TIMEOUT="${POLL_TIMEOUT:-1800}"
POLL_INTERVAL="${POLL_INTERVAL:-2}"

# API key: environment wins, then .env. Empty = auth disabled (local dev default).
if [ -z "${API_KEY:-}" ] && [ -f "$ROOT_DIR/.env" ]; then
    API_KEY="$(sed -n 's/^[[:space:]]*API_KEY[[:space:]]*=//p' "$ROOT_DIR/.env" | tail -n 1 | tr -d '\r')"
    API_KEY="${API_KEY#\"}"; API_KEY="${API_KEY%\"}"
    API_KEY="${API_KEY#\'}"; API_KEY="${API_KEY%\'}"
    API_KEY="$(printf '%s' "$API_KEY" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
fi
AUTH=()
if [ -n "${API_KEY:-}" ]; then
    AUTH=(-H "X-API-Key: ${API_KEY}")
fi

# Python 3 for JSON parsing: system python3/python first, project uv as fallback.
if command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD=(python3)
elif command -v python >/dev/null 2>&1 && \
     python -c 'import sys; sys.exit(0 if sys.version_info[0] == 3 else 1)' 2>/dev/null; then
    PYTHON_CMD=(python)
elif command -v uv >/dev/null 2>&1; then
    PYTHON_CMD=(uv run python)
else
    echo "[FAIL] Python 3 not found (need python3 or uv)" >&2
    exit 1
fi

py() { "${PYTHON_CMD[@]}" "$@"; }

say()  { printf '\n== %s ==\n' "$*"; }
ok()   { printf '[ OK ] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*"; }
die()  { printf '[FAIL] %s\n' "$*" >&2; exit 1; }

# Read a dotted path from a JSON document on stdin. Lists use numeric segments.
json_get() {
    py -c '
import json, sys
value = json.load(sys.stdin)
for key in sys.argv[1].split("."):
    value = value[int(key)] if isinstance(value, list) else value[key]
if value is None:
    print("")
elif isinstance(value, (dict, list)):
    print(json.dumps(value, ensure_ascii=False))
else:
    print(value)
' "$1"
}

json_opt() { json_get "$1" 2>/dev/null || true; }

print_json() {
    local body="$1"
    printf '%s' "$body" | py -c '
import json, sys
print(json.dumps(json.load(sys.stdin), ensure_ascii=False, indent=2))
' 2>/dev/null || printf '%s\n' "$body"
}

# api METHOD PATH [JSON_BODY] -> response body on stdout.
api() {
    local method="$1" path="$2" body="${3:-}"
    local args=(-sS --max-time 120 -X "$method" "${API_BASE}${path}" "${AUTH[@]}")
    if [ -n "$body" ]; then
        args+=(-H 'Content-Type: application/json' -d "$body")
    fi
    curl "${args[@]}"
}

# Fail unless the unified envelope reports code "200".
envelope_ok() {
    local body="$1" what="$2" code
    code="$(printf '%s' "$body" | json_opt code)"
    if [ "$code" != "200" ]; then
        print_json "$body"
        die "$what failed (envelope code=${code:-none})"
    fi
}

# Poll /jobs/{id}; state must reach done (failed = abort).
wait_job() {
    local job_id="$1" started=$SECONDS last=""
    while :; do
        local body state progress step
        body="$(api GET "/jobs/${job_id}")"
        state="$(printf '%s' "$body" | json_opt data.state)"
        progress="$(printf '%s' "$body" | json_opt data.progress)"
        step="$(printf '%s' "$body" | json_opt data.step)"
        if [ "${state}:${step}" != "$last" ]; then
            printf '  job %s  state=%-9s progress=%s  %s\n' \
                "$job_id" "${state:-?}" "${progress:-?}" "${step:-}"
            last="${state}:${step}"
        fi
        case "$state" in
            done)
                ok "job ${job_id} done"
                return 0
                ;;
            failed)
                print_json "$body"
                die "job ${job_id} failed: $(printf '%s' "$body" | json_opt data.error)"
                ;;
        esac
        [ -n "$state" ] || die "cannot read state of job ${job_id}"
        [ $((SECONDS - started)) -lt "$POLL_TIMEOUT" ] || \
            die "job ${job_id} timed out after ${POLL_TIMEOUT}s"
        sleep "$POLL_INTERVAL"
    done
}

# Poll /regulations/{docId}/result; inner code 1 = done, 0 = failed.
wait_result() {
    local doc_id="$1" started=$SECONDS last=""
    while :; do
        local body code msg stages
        body="$(api GET "/regulations/${doc_id}/result")"
        envelope_ok "$body" "result pull for ${doc_id}"
        code="$(printf '%s' "$body" | json_opt data.code)"
        msg="$(printf '%s' "$body" | json_opt data.msg)"
        stages="$(printf '%s' "$body" | json_opt data.stages)"
        if [ "${code}:${stages}" != "$last" ]; then
            printf '  docId=%s  code=%s  msg=%s\n  stages=%s\n' \
                "$doc_id" "${code:-?}" "${msg:-}" "${stages:-}"
            last="${code}:${stages}"
        fi
        case "$code" in
            1)
                ok "result ready for docId=${doc_id}"
                return 0
                ;;
            0)
                print_json "$body"
                die "chain failed for docId=${doc_id}: ${msg}"
                ;;
            2)
                ;;
            *)
                warn "unexpected result code=${code:-none}"
                ;;
        esac
        [ $((SECONDS - started)) -lt "$POLL_TIMEOUT" ] || \
            die "result for docId=${doc_id} timed out after ${POLL_TIMEOUT}s"
        sleep "$POLL_INTERVAL"
    done
}

# First non-hidden regular file in a directory (empty output if none).
first_material() {
    local dir="$1" f
    for f in "$dir"/*; do
        [ -f "$f" ] || continue
        case "$(basename "$f")" in .*) continue ;; esac
        printf '%s\n' "$f"
        return 0
    done
    return 1
}

# Print one-line regulation summary and its artifacts.
show_regulation() {
    local reg_id="$1" body stages article_count clauses artifacts
    body="$(api GET "/regulations/${reg_id}")"
    envelope_ok "$body" "regulation detail"
    stages="$(printf '%s' "$body" | json_opt data.stages)"
    article_count="$(printf '%s' "$body" | json_opt data.article_count)"
    clauses="$(api GET "/regulations/${reg_id}/clauses?limit=1")"
    artifacts="$(api GET "/regulations/${reg_id}/artifacts")"

    printf '\n  regulation_id : %s\n' "$reg_id"
    printf '  article_count : %s\n' "${article_count:-?}"
    printf '  stages        : %s\n' "${stages:-?}"
    printf '  clause_total  : %s\n' "$(printf '%s' "$clauses" | json_opt data.total)"
    printf '  artifacts     :\n'
    print_json "$artifacts" | sed 's/^/    /'
}
