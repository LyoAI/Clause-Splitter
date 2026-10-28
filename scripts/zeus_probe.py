#!/usr/bin/env python3
"""Standalone Zeus knowledge-base probe — no service, no Mongo, no LLM.

Purpose: when a Zeus call fails, the application logs collapse the failure into
"Zeus 业务错误: error" and the actual response body is lost. This script talks to
Zeus directly, one endpoint per subcommand, and prints the raw status code,
response headers and full body so the real reason is visible.

It also reproduces the two upload details that differ between our client and a
working Postman request:

  * the ``Content-Type`` of the multipart file part (we send
    ``application/octet-stream``; Postman derives it from the extension), and
  * the ``filename`` encoding (ASCII vs Chinese).

Examples:
    # credentials from .env / shell, then just probe each step
    python scripts/zeus_probe.py search --query "数据安全"

    # full chain with the file that failed in production
    python scripts/zeus_probe.py all --file "TEST-20261027-001.docx"

    # A/B the part Content-Type against a real Zeus
    python scripts/zeus_probe.py upload --file 条例.docx --part-ctype octet
    python scripts/zeus_probe.py upload --file 条例.docx --part-ctype auto

    # what would be sent, without sending it
    python scripts/zeus_probe.py all --file 条例.docx --dry-run
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"

UPLOAD = "/api/v1/uns/text_data_process/app/file"
DOC = "/api/v1/uns/text_data_process/app/doc/{index}"
DOC_BATCH = "/api/v1/uns/text_data_process/app/doc/batch/{index}"
SEARCH = "/api/v1/uns/zeus-search/{app_id}/server/standard_search"

LABEL_PREFIX = "doc:"
_BODY_LIMIT = 4000


# ── credentials ─────────────────────────────────────────────────────────


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


class Zeus:
    """Minimal Zeus client that records everything it sends and receives."""

    def __init__(
        self, base: str, app_id: str, secret: str, index: str,
        *, timeout: float = 60.0, created_by: str = "clause_splitter",
    ) -> None:
        self.base = (base or "").rstrip("/")
        self.app_id = app_id
        self.secret = secret
        self.index = index
        self.timeout = timeout
        self.created_by = created_by
        self.session = requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self.base and self.app_id and self.secret and self.index)

    def _headers(self, request_id: str) -> dict[str, str]:
        return {
            "x-app-id": self.app_id,
            "x-app-secret": self.secret,
            "x-index": self.index,
            "x-request-id": request_id,
        }

    def call(self, method: str, path: str, *, dry_run: bool = False, timeout: float | None = None, **kwargs: Any) -> requests.Response | None:
        """One request; prints what went out and everything that came back."""
        url = self.base + path
        request_id = str(uuid.uuid4())
        headers = self._headers(request_id)
        shown = {k: ("***" if k.lower() == "x-app-secret" else v) for k, v in headers.items()}

        print(f"\n{'=' * 70}")
        print(f"{method} {url}")
        print(f"  request-id: {request_id}")
        for key, value in shown.items():
            print(f"  {key}: {value}")
        for key in ("json", "files", "data"):
            if key in kwargs:
                print(f"  body ({key}): {_preview(kwargs[key])}")

        if dry_run:
            print("  [dry-run] not sent")
            return None

        started = time.monotonic()
        try:
            resp = self.session.request(
                method, url, headers=headers, timeout=timeout or self.timeout, **kwargs,
            )
        except requests.RequestException as exc:
            print(f"  ✗ transport error after {time.monotonic() - started:.2f}s: {exc}")
            raise SystemExit(2)
        elapsed = time.monotonic() - started

        print(f"  ← HTTP {resp.status_code} in {elapsed:.2f}s")
        for key in ("Content-Type", "x-request-id", "trace-id", "X-Trace-Id"):
            if key in resp.headers:
                print(f"    {key}: {resp.headers[key]}")
        print(f"  body:\n{_body(resp)}")

        verdict = _verdict(resp)
        print(f"  => {verdict}")
        return resp

    # ── endpoints ───────────────────────────────────────────────────────

    def upload(self, path: Path, *, part_ctype: str, filename: str | None = None, dry_run: bool = False):
        """POST the file as multipart/form-data (the step that fails in prod)."""
        name = filename or path.name
        ctype = _part_content_type(path, name, part_ctype)
        print(f"  file: {name} ({path.stat().st_size / 1024:.1f} KB)")
        print(f"  part Content-Type: {ctype}")
        with path.open("rb") as fh:
            return self.call(
                "POST", UPLOAD, dry_run=dry_run, timeout=max(self.timeout, 300.0),
                files={"file": (name, fh, ctype)},
            )

    def submit(
        self, url: str, filename: str, *, labels: list[str], org_id: str,
        file_size: int = 0, dry_run: bool = False,
    ):
        """Hand Zeus the uploaded file URL so it parses / chunks / embeds it."""
        payload = [{
            "org_data_id": org_id,
            "file_name": filename,
            "file_type": (Path(filename).suffix or "").lstrip(".").lower(),
            "file_size": file_size,
            "url": url,
            "text_labels": labels,
            "created_by": self.created_by,
        }]
        return self.call(
            "POST", DOC.format(index=_q(self.index)), dry_run=dry_run, json=payload,
        )

    def search(self, query: str, *, k: int, labels: list[str], min_score: float, dry_run: bool = False):
        body = {
            "session_id": str(uuid.uuid4()),
            "query": query,
            "kb": [{
                "alias": self.index,
                "filter": {"data_type": ["doc"], "text_labels": list(labels), "min_score": min_score},
                "limit": max(k, 20),
            }],
            "limit": k,
        }
        return self.call(
            "POST", SEARCH.format(app_id=_q(self.app_id)), dry_run=dry_run, json=body,
        )

    def delete(self, org_ids: list[str], *, dry_run: bool = False):
        return self.call(
            "DELETE", DOC_BATCH.format(index=_q(self.index)), dry_run=dry_run, json=org_ids,
        )


# ── helpers ─────────────────────────────────────────────────────────────


def _q(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe="")


def _part_content_type(path: Path, name: str, mode: str) -> str:
    if mode == "octet":
        return "application/octet-stream"
    if mode == "auto":
        return mimetypes.guess_type(name)[0] or "application/octet-stream"
    return mode  # literal


def _preview(value: Any) -> str:
    if isinstance(value, dict) and "file" in value:
        name, fh, ctype = value["file"]
        return f"multipart file={name!r} content-type={ctype!r} <binary>"
    try:
        return json.dumps(value, ensure_ascii=False)[:800]
    except (TypeError, ValueError):
        return repr(value)[:800]


def _body(resp: requests.Response) -> str:
    text = resp.text
    if not text:
        return "    <empty>"
    try:
        pretty = json.dumps(resp.json(), ensure_ascii=False, indent=2)
    except ValueError:
        pretty = text
    if len(pretty) > _BODY_LIMIT:
        pretty = pretty[:_BODY_LIMIT] + f"\n    … ({len(pretty)} chars total)"
    return "\n".join("    " + line for line in pretty.splitlines())


def _verdict(resp: requests.Response) -> str:
    """Human summary — HTTP status plus the envelope's status field."""
    code = resp.status_code
    try:
        body = resp.json()
    except ValueError:
        body = None
    status = str((body or {}).get("status") or "").strip().lower() if isinstance(body, dict) else ""
    msg = str((body or {}).get("msg") or "") if isinstance(body, dict) else ""

    if code in (401, 403):
        return f"✗ 鉴权失败({code}) — 检查 x-app-id / x-app-secret / x-index"
    if code == 404:
        return f"✗ 404 — 路径或索引不存在（{msg or resp.text[:80]}）"
    if code >= 500:
        return f"✗ Zeus 侧错误({code}) — {msg or resp.text[:80]}"
    if code >= 400:
        return f"✗ 请求被拒({code}) — {msg or resp.text[:80]}"
    if status and status != "success":
        return f"✗ HTTP 200 但业务失败 status={status!r} msg={msg!r} —— 看上面的完整响应体"
    return "✓ 成功"


# ── commands ────────────────────────────────────────────────────────────


def cmd_upload(z: Zeus, args) -> None:
    path = Path(args.file)
    if not path.is_file():
        raise SystemExit(f"file not found: {path}")
    resp = z.upload(
        path, part_ctype=args.part_ctype, filename=args.filename, dry_run=args.dry_run,
    )
    if resp is not None and args.json_out:
        (Path(args.json_out)).write_text(resp.text, encoding="utf-8")
        print(f"\n  raw body saved to {args.json_out}")


def cmd_submit(z: Zeus, args) -> None:
    labels = _labels(args.label)
    resp = z.submit(
        args.url, Path(args.filename).name,
        labels=[*labels, LABEL_PREFIX + Path(args.filename).stem],
        org_id=args.org_id or uuid.uuid4().hex, dry_run=args.dry_run,
    )
    if resp is not None and args.json_out:
        Path(args.json_out).write_text(resp.text, encoding="utf-8")


def cmd_search(z: Zeus, args) -> None:
    z.search(
        args.query, k=args.k, labels=_labels(args.label),
        min_score=args.min_score, dry_run=args.dry_run,
    )


def cmd_delete(z: Zeus, args) -> None:
    z.delete(args.org_id, dry_run=args.dry_run)


def cmd_ping(z: Zeus, args) -> None:
    """Cheapest auth check: a one-word search that is expected to return few rows."""
    resp = z.search("测试", k=1, labels=[], min_score=0.0, dry_run=args.dry_run)
    if resp is None:
        return
    print("\n  说明: 401/403 = 凭据问题；200 + status=success = 连通且鉴权正常；")
    print("        404 = 该应用没开通检索（不是网络问题）。")


def cmd_all(z: Zeus, args) -> None:
    path = Path(args.file)
    if not path.is_file():
        raise SystemExit(f"file not found: {path}")
    stem = Path(args.filename or path.name).stem
    labels = _labels(args.label) or ["internal-regulation"]
    org_id = args.org_id or uuid.uuid4().hex

    print(f"\n### 1/3 上传文件 (part Content-Type={args.part_ctype})")
    up = z.upload(path, part_ctype=args.part_ctype, filename=args.filename, dry_run=args.dry_run)
    if up is None:
        return
    if not _is_ok(up):
        print("\n上传就失败了 —— 后面两步没有意义。上面的响应体就是原因。")
        print("对照 Postman：把 --part-ctype 换成 auto 再跑一次，看是否变成成功。")
        raise SystemExit(1)
    url = _data(up)
    if not isinstance(url, str) or not url.strip():
        print("\n上传返回 success 但 data 里没有文件 URL —— Zeus 侧可能改了协议。")
        raise SystemExit(1)

    print(f"\n### 2/3 提交入库 (url={url})")
    sub = z.submit(url.strip(), Path(args.filename or path.name).name,
                   labels=[*labels, LABEL_PREFIX + stem], org_id=org_id,
                   file_size=path.stat().st_size, dry_run=args.dry_run)
    if sub is None or not _is_ok(sub):
        print("\n提交入库失败。")
        raise SystemExit(1)

    print(f"\n### 3/3 等待可检索 (最多 {args.wait}s，每 {args.interval}s 查一次)")
    print("   Zeus 后台解析/分块/embedding 是异步的，提交成功 ≠ 立刻能搜到。")
    deadline = time.monotonic() + args.wait
    query = args.query or f"{stem} 第一条"
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        print(f"\n  -- 第 {attempt} 次检索: {query!r}")
        resp = z.search(query, k=5, labels=labels, min_score=args.min_score)
        hits = _data(resp) if resp is not None else None
        if isinstance(hits, list) and hits:
            print(f"\n  ✓ 已可检索，命中 {len(hits)} 条 —— 入库链路完整。")
            return
        if args.dry_run:
            return
        time.sleep(args.interval)
    print(f"\n  ⚠ {args.wait}s 内未检索到内容。可能仍在索引，或标签/query 不匹配；")
    print("    用 python scripts/zeus_probe.py search --query ... --label ... 手动复查。")


def _labels(raw: list[str] | None) -> list[str]:
    return [s.strip() for s in (raw or []) if s.strip()]


def _is_ok(resp: requests.Response) -> bool:
    if resp.status_code >= 400:
        return False
    try:
        body = resp.json()
    except ValueError:
        return False
    return isinstance(body, dict) and str(body.get("status") or "").lower() == "success"


def _data(resp: requests.Response) -> Any:
    try:
        return resp.json().get("data")
    except (ValueError, AttributeError):
        return None


# ── cli ─────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--base", default=_env_value("RAG_API_BASE"), help="Zeus base URL")
    parser.add_argument("--app-id", default=_env_value("ZEUS_APP_ID"))
    parser.add_argument("--app-secret", default=_env_value("ZEUS_APP_SECRET"))
    parser.add_argument("--index", default=_env_value("ZEUS_INDEX"))
    parser.add_argument("--created-by", default=_env_value("ZEUS_CREATED_BY") or "clause_splitter")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--dry-run", action="store_true", help="print the request, send nothing")
    parser.add_argument("--json-out", metavar="FILE", help="save the raw response body")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ping", help="cheapest auth/connectivity check (a trivial search)")
    p.set_defaults(func=cmd_ping)

    p = sub.add_parser("upload", help="POST /file — the multipart upload")
    p.add_argument("--file", required=True)
    p.add_argument("--filename", help="override the filename inside the multipart part")
    p.add_argument("--part-ctype", default="auto",
                   choices=("auto", "octet"),
                   help="multipart part Content-Type: auto=按后缀推(同 Postman), octet=application/octet-stream(线上代码)")
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser("submit", help="POST /doc/{index} — register the uploaded URL")
    p.add_argument("--url", required=True, help="the file URL returned by upload")
    p.add_argument("--filename", required=True)
    p.add_argument("--label", action="append", help="text_label, repeatable")
    p.add_argument("--org-id", default="")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("search", help="POST standard_search")
    p.add_argument("--query", required=True)
    p.add_argument("--label", action="append", help="text_labels filter, repeatable")
    p.add_argument("--k", type=int, default=5)
    p.add_argument("--min-score", type=float, default=0.0)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("delete", help="DELETE /doc/batch/{index}")
    p.add_argument("--org-id", required=True)
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("all", help="upload → submit → poll until searchable")
    p.add_argument("--file", required=True)
    p.add_argument("--filename", help="override the filename inside the multipart part")
    p.add_argument("--part-ctype", default="auto", choices=("auto", "octet"))
    p.add_argument("--label", action="append")
    p.add_argument("--org-id", default="")
    p.add_argument("--query", help="query used to poll for the document")
    p.add_argument("--min-score", type=float, default=0.0)
    p.add_argument("--wait", type=float, default=180.0, help="how long to poll (s)")
    p.add_argument("--interval", type=float, default=15.0)
    p.set_defaults(func=cmd_all)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not (args.base and args.app_id and args.app_secret and args.index):
        missing = [
            name for name, value in (
                ("RAG_API_BASE", args.base), ("ZEUS_APP_ID", args.app_id),
                ("ZEUS_APP_SECRET", args.app_secret), ("ZEUS_INDEX", args.index),
            ) if not value
        ]
        raise SystemExit(
            f"缺少配置: {', '.join(missing)}\n"
            f"在 {ENV_FILE} 里设置，或用 --base/--app-id/--app-secret/--index 传入。"
        )
    zeus = Zeus(
        args.base, args.app_id, args.app_secret, args.index,
        timeout=args.timeout, created_by=args.created_by,
    )
    if zeus.base.startswith("http://"):
        print("注意: 使用明文 http，凭据会以明文发送（内网环境通常如此）。", file=sys.stderr)
    args.func(zeus, args)


if __name__ == "__main__":
    main()
