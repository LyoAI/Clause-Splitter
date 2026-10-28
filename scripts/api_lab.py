#!/usr/bin/env python3
"""Manual API lab for a running Clause_Splitter service.

Each subcommand maps to one HTTP endpoint, so you can drive the service
step by step and watch its logs in another terminal.

Examples:
    python scripts/api_lab.py health
    python scripts/api_lab.py upload upload_materials/external/办法.docx
    python scripts/api_lab.py run <reg_id>
    python scripts/api_lab.py wait <job_id> --timeout 1800
    python scripts/api_lab.py clauses <reg_id> --status 缺失
    python scripts/api_lab.py chain upload_materials/external/办法.docx

Intranet / remote service:
    python scripts/api_lab.py --base http://10.0.0.8:18998/api/v1/clause_splitter health
    API_KEY=xxx python scripts/api_lab.py ...

Run ``python scripts/api_lab.py <command> -h`` for per-command options.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import quote

import requests

DEFAULT_BASE = "http://127.0.0.1:18998/api/v1/clause_splitter"
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _env_value(key: str) -> str:
    """Shell env first, then the repo ``.env`` (values are never printed)."""
    value = os.environ.get(key)
    if value:
        return value
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            name, sep, raw = line.strip().partition("=")
            if sep and name == key:
                return raw.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


class Client:
    def __init__(self, base: str, api_key: str = "", timeout: float = 120.0) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        if api_key:
            self.session.headers["X-API-Key"] = api_key

    def call(self, method: str, path: str, *, envelope: bool = True, quiet: bool = False, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        resp = self.session.request(method, f"{self.base}{path}", **kwargs)
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        if not quiet:
            print(f"\n{method} {path} -> HTTP {resp.status_code}")
            if isinstance(body, (dict, list)):
                print(json.dumps(body, ensure_ascii=False, indent=2))
            else:
                print(body)
        if resp.status_code >= 400:
            raise SystemExit(1)
        if envelope and isinstance(body, dict) and body.get("status") == "error":
            raise SystemExit(1)
        return body

    def data(self, method: str, path: str, **kwargs):
        body = self.call(method, path, **kwargs)
        return body.get("data") if isinstance(body, dict) else body


def _get_job(client: Client, job_id: str) -> dict:
    body = client.call("GET", f"/jobs/{job_id}", quiet=True)
    return body.get("data") or {}


def wait_job(client: Client, job_id: str, timeout: float, interval: float) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        job = _get_job(client, job_id)
        state = job.get("state", "")
        current = (state, round(float(job.get("progress") or 0), 2), job.get("step") or "")
        if current != last:
            print(f"  job {job_id}: {state} {current[1]:.0%} {current[2]}")
            last = current
        if state in ("done", "failed"):
            if job.get("error"):
                print(f"  error: {job['error']}")
            return job
        time.sleep(interval)
    raise SystemExit(f"job {job_id} not finished within {timeout}s")


def wait_result(client: Client, doc_id: str, timeout: float, interval: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.call("GET", f"/regulations/{doc_id}/result", quiet=True, envelope=False)
        if str(body.get("code")) in ("0", "1"):
            print(json.dumps(body, ensure_ascii=False, indent=2))
            return body
        print(f"  docId {doc_id}: code={body.get('code')} msg={body.get('msg')}")
        time.sleep(interval)
    raise SystemExit(f"result for {doc_id} not ready within {timeout}s")


def cmd_health(client: Client, args) -> None:
    # healthz is mounted at the app root, outside the API prefix.
    root = re.sub(r"/api/v\d+/[^/]+/?$", "", client.base)
    resp = client.session.get(f"{root}/healthz", timeout=10)
    print(f"GET {root}/healthz -> HTTP {resp.status_code} {resp.text}")


def cmd_upload(client: Client, args) -> None:
    path = Path(args.file)
    with path.open("rb") as fh:
        data = client.data(
            "POST", "/regulations",
            files={"file": (path.name, fh, "application/octet-stream")},
            data={"title": args.title} if args.title else None,
        )
    print(f"\nregulation_id = {data.get('id')}")


def cmd_list(client: Client, args) -> None:
    client.call("GET", f"/regulations?limit={args.limit}&offset={args.offset}")


def cmd_get(client: Client, args) -> None:
    client.call("GET", f"/regulations/{args.reg_id}")


def cmd_delete(client: Client, args) -> None:
    client.call("DELETE", f"/regulations/{args.reg_id}")


def cmd_stage(client: Client, args, stage: str, body: dict | None = None) -> None:
    data = client.data("POST", f"/regulations/{args.reg_id}/{stage}", json=body or {})
    job_id = data["id"]
    print(f"\njob_id = {job_id}")
    if args.wait:
        wait_job(client, job_id, args.timeout, args.interval)


def cmd_split(client: Client, args) -> None:
    cmd_stage(client, args, "split", {"force": True} if args.force else {})


def cmd_patch_applicability(client: Client, args) -> None:
    client.call(
        "PATCH", f"/regulations/{args.reg_id}/applicability",
        json={"applicable": args.applicable == "true", "reason": args.reason},
    )


def cmd_clauses(client: Client, args) -> None:
    params = [f"limit={args.limit}", f"offset={args.offset}"]
    if args.decision:
        params.append(f"decision={args.decision}")
    if args.status:
        params.append(f"status={args.status}")
    if args.include_candidates:
        params.append("include=candidates")
    client.call("GET", f"/regulations/{args.reg_id}/clauses?{'&'.join(params)}")


def cmd_clause(client: Client, args) -> None:
    client.call("GET", f"/regulations/{args.reg_id}/clauses/{args.clause_id}")


def cmd_jobs(client: Client, args) -> None:
    if args.reg_id:
        client.call("GET", f"/regulations/{args.reg_id}/jobs?limit={args.limit}")
    else:
        params = [f"limit={args.limit}"]
        if args.state:
            params.append(f"state={args.state}")
        if args.job_type:
            params.append(f"job_type={args.job_type}")
        client.call("GET", f"/jobs?{'&'.join(params)}")


def cmd_job(client: Client, args) -> None:
    client.call("GET", f"/jobs/{args.job_id}")


def cmd_wait(client: Client, args) -> None:
    wait_job(client, args.job_id, args.timeout, args.interval)


def cmd_artifacts(client: Client, args) -> None:
    client.call("GET", f"/regulations/{args.reg_id}/artifacts")


def cmd_artifact(client: Client, args) -> None:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = args.name
    if name.startswith("episodes/"):
        path = f"/regulations/{args.reg_id}/artifacts/episodes/{quote(name[9:], safe='')}"
    else:
        path = f"/regulations/{args.reg_id}/artifacts/{quote(name, safe='')}"
    resp = client.session.get(f"{client.base}{path}", timeout=client.timeout)
    resp.raise_for_status()
    target = out_dir / Path(args.name).name
    target.write_bytes(resp.content)
    print(f"saved {target} ({len(resp.content)} bytes)")


def cmd_lib_upload(client: Client, args) -> None:
    path = Path(args.file)
    with path.open("rb") as fh:
        data = client.data(
            "POST", "/library/documents",
            files={"file": (path.name, fh, "application/octet-stream")},
        )
    print(f"\ndoc_id = {data.get('id')} state={data.get('remote_state')} chunks={data.get('chunk_count')}")


def cmd_lib_list(client: Client, args) -> None:
    client.call("GET", "/library/documents")


def cmd_lib_delete(client: Client, args) -> None:
    client.call("DELETE", f"/library/documents/{args.doc_id}")


def cmd_lib_index(client: Client, args) -> None:
    data = client.data("POST", "/library/index")
    job_id = data.get("job_id")
    print(f"\njob_id = {job_id}")
    if args.wait and job_id:
        wait_job(client, job_id, args.timeout, args.interval)
        client.call("GET", "/library/index")


def cmd_lib_status(client: Client, args) -> None:
    client.call("GET", "/library/index")


def cmd_push(client: Client, args) -> None:
    payload = {"docId": args.doc_id, "optType": args.opt_type, "taskId": args.task_id}
    if args.doc_type is not None:
        payload["docType"] = args.doc_type
    if args.path:
        payload["doclobsUrl"] = str(Path(args.path).resolve())
    elif args.url:
        payload["doclobsUrl"] = args.url
    elif args.content is not None:
        payload["docContent"] = args.content
    else:
        raise SystemExit("push needs one of --path / --url / --content")
    data = client.data("POST", "/regulations/push", json=payload)
    print(f"\njob_id = {data.get('job_id')} dispatch={data.get('dispatch')} manual={data.get('manual')}")
    # The push response is a fixed backend contract; read the routing back from
    # the regulation instead of expecting extra fields in it.
    if data.get("regulation_id"):
        reg = client.data("GET", f"/regulations/{data['regulation_id']}", quiet=True)
        kind = reg.get("doc_kind") or "external"
        chain = "内规/附件(入库+拆条)" if kind in ("internal", "attach") else "外规(四阶段)"
        print(f"docType={args.doc_type or '(缺失)'} → kind={kind} 链路={chain}")
    if args.wait_result:
        wait_result(client, args.doc_id, args.timeout, args.interval)


def cmd_result(client: Client, args) -> None:
    body = client.call("GET", f"/regulations/{args.doc_id}/result", envelope=False)
    if isinstance(body, dict) and body.get("data") is not None:
        print(f"\nclauses = {len(body['data'])}")


def cmd_chain(client: Client, args) -> None:
    path = Path(args.file)
    with path.open("rb") as fh:
        reg = client.data(
            "POST", "/regulations",
            files={"file": (path.name, fh, "application/octet-stream")},
            data={"title": args.title} if args.title else None,
        )
    reg_id = reg["id"]
    print(f"\nregulation_id = {reg_id}")
    job = client.data("POST", f"/regulations/{reg_id}/run")
    job_id = job["id"]
    print(f"job_id = {job_id}")
    finished = wait_job(client, job_id, args.timeout, args.interval)
    client.call("GET", f"/regulations/{reg_id}")
    if finished.get("state") == "done":
        client.call("GET", f"/regulations/{reg_id}/artifacts")


def _add_wait(p: argparse.ArgumentParser) -> None:
    p.add_argument("--wait", action="store_true", help="poll the job until it finishes")
    p.add_argument("--timeout", type=float, default=1800, help="poll deadline (s)")
    p.add_argument("--interval", type=float, default=3, help="poll interval (s)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=os.environ.get("BASE_URL", DEFAULT_BASE))
    parser.add_argument("--api-key", default=_env_value("API_KEY"))
    parser.add_argument("--timeout", type=float, default=120, help="HTTP timeout (s)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("health", help="GET /healthz")
    p.set_defaults(func=cmd_health)

    p = sub.add_parser("upload", help="POST /regulations")
    p.add_argument("file")
    p.add_argument("--title")
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser("list", help="GET /regulations")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--offset", type=int, default=0)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("get", help="GET /regulations/{id}")
    p.add_argument("reg_id")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("delete", help="DELETE /regulations/{id}")
    p.add_argument("reg_id")
    p.set_defaults(func=cmd_delete)

    for name, stage in (
        ("apply", "applicability"), ("internalize", "internalization"), ("analyze", "analysis"),
    ):
        p = sub.add_parser(name, help=f"POST /regulations/{{id}}/{stage}")
        p.add_argument("reg_id")
        _add_wait(p)
        p.set_defaults(func=lambda c, a, s=stage: cmd_stage(c, a, s))

    p = sub.add_parser("split", help="POST /regulations/{id}/split")
    p.add_argument("reg_id")
    p.add_argument("--force", action="store_true")
    _add_wait(p)
    p.set_defaults(func=cmd_split)

    p = sub.add_parser("run", help="POST /regulations/{id}/run (whole chain)")
    p.add_argument("reg_id")
    p.add_argument("--wait", action=argparse.BooleanOptionalAction, default=True,
                   help="poll the job until it finishes (default on)")
    p.add_argument("--timeout", type=float, default=1800, help="poll deadline (s)")
    p.add_argument("--interval", type=float, default=3, help="poll interval (s)")
    p.set_defaults(func=lambda c, a: cmd_stage(c, a, "run"))

    p = sub.add_parser("patch-applicability", help="PATCH /regulations/{id}/applicability")
    p.add_argument("reg_id")
    p.add_argument("--applicable", choices=("true", "false"), required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_patch_applicability)

    p = sub.add_parser("clauses", help="GET /regulations/{id}/clauses")
    p.add_argument("reg_id")
    p.add_argument("--decision")
    p.add_argument("--status")
    p.add_argument("--include-candidates", action="store_true")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--offset", type=int, default=0)
    p.set_defaults(func=cmd_clauses)

    p = sub.add_parser("clause", help="GET /regulations/{id}/clauses/{clause_id}")
    p.add_argument("reg_id")
    p.add_argument("clause_id")
    p.set_defaults(func=cmd_clause)

    p = sub.add_parser("jobs", help="GET /jobs or /regulations/{id}/jobs")
    p.add_argument("reg_id", nargs="?")
    p.add_argument("--state")
    p.add_argument("--job-type")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_jobs)

    p = sub.add_parser("job", help="GET /jobs/{job_id}")
    p.add_argument("job_id")
    p.set_defaults(func=cmd_job)

    p = sub.add_parser("wait", help="poll GET /jobs/{job_id}")
    p.add_argument("job_id")
    p.add_argument("--timeout", type=float, default=1800)
    p.add_argument("--interval", type=float, default=3)
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("artifacts", help="GET /regulations/{id}/artifacts")
    p.add_argument("reg_id")
    p.set_defaults(func=cmd_artifacts)

    p = sub.add_parser("artifact", help="download one artifact")
    p.add_argument("reg_id")
    p.add_argument("name")
    p.add_argument("--out", default=".")
    p.set_defaults(func=cmd_artifact)

    p = sub.add_parser("lib-upload", help="POST /library/documents")
    p.add_argument("file")
    p.set_defaults(func=cmd_lib_upload)

    p = sub.add_parser("lib-list", help="GET /library/documents")
    p.set_defaults(func=cmd_lib_list)

    p = sub.add_parser("lib-delete", help="DELETE /library/documents/{id}")
    p.add_argument("doc_id")
    p.set_defaults(func=cmd_lib_delete)

    p = sub.add_parser("lib-index", help="POST /library/index")
    _add_wait(p)
    p.set_defaults(func=cmd_lib_index)

    p = sub.add_parser("lib-status", help="GET /library/index")
    p.set_defaults(func=cmd_lib_status)

    p = sub.add_parser("push", help="POST /regulations/push")
    p.add_argument("--doc-id", required=True)
    p.add_argument("--path", help="local file, sent as doclobsUrl")
    p.add_argument("--url", help="http(s) URL, sent as doclobsUrl")
    p.add_argument("--content", help="plain text, sent as docContent")
    p.add_argument("--opt-type", type=int, default=0, choices=(0, 1))
    p.add_argument("--doc-type", default=None,
                   help="backend docType: 1=外规 2=内规 3=附件 (omit to test the missing case)")
    p.add_argument("--task-id", default="task-local")
    p.add_argument("--wait-result", action="store_true")
    p.add_argument("--timeout", type=float, default=1800)
    p.add_argument("--interval", type=float, default=3)
    p.set_defaults(func=cmd_push)

    p = sub.add_parser("result", help="GET /regulations/{docId}/result")
    p.add_argument("doc_id")
    p.set_defaults(func=cmd_result)

    p = sub.add_parser("chain", help="upload + run + wait + print artifacts")
    p.add_argument("file")
    p.add_argument("--title")
    p.add_argument("--timeout", type=float, default=1800)
    p.add_argument("--interval", type=float, default=3)
    p.set_defaults(func=cmd_chain)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    client = Client(args.base, args.api_key, args.timeout)
    args.func(client, args)


if __name__ == "__main__":
    main()
