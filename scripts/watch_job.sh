#!/usr/bin/env bash
# Poll one job until done/failed. Useful in a second terminal.
#
# Usage: ./scripts/watch_job.sh <job_id>

set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_common.sh
source "$SCRIPT_DIR/_common.sh"

JOB_ID="${1:-}"
if [ -z "$JOB_ID" ]; then
    printf 'Usage: %s <job_id>\n' "$0" >&2
    exit 2
fi

wait_job "$JOB_ID"
body="$(api GET "/jobs/${JOB_ID}")"
print_json "$body"
