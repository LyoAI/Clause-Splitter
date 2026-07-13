#!/usr/bin/env python3
"""End-to-end test script for Clause Splitter API.

Usage:
    # Start the server first:
    uv run main.py

    # 1. Split only
    python test_app.py --split --file upload_materials/外部法规.pdf

    # 2. Compare only (needs a completed split task)
    python test_app.py --compare --split-task-id abc123 --internal-file 内规拆分结果.json

    # 3. Summary only (needs a completed compare task)
    python test_app.py --summary --compare-task-id def456

    # 4. Full pipeline
    python test_app.py --pipeline --file 外部法规.pdf --internal-file 内规拆分结果.json

    # Override server URL
    python test_app.py --split --file doc.pdf --base-url http://127.0.0.1:9000/api/v1
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

BASE_URL = os.environ.get("CLAUSE_SPLITTER_URL", "http://localhost:8000/api/v1")
OUTPUT_ROOT = Path(__file__).parent / "outputs"
POLL_INTERVAL = 2.0  # seconds between status checks


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _post(endpoint: str, **kwargs) -> dict:
    resp = requests.post(f"{BASE_URL}{endpoint}", **kwargs)
    resp.raise_for_status()
    return resp.json()


def _get(endpoint: str) -> dict:
    resp = requests.get(f"{BASE_URL}{endpoint}")
    resp.raise_for_status()
    return resp.json()


def _get_raw(endpoint: str) -> bytes:
    resp = requests.get(f"{BASE_URL}{endpoint}")
    resp.raise_for_status()
    return resp.content


def poll_task(task_id: str, label: str = "") -> dict:
    """Poll task status until completed or failed. Returns final status JSON."""
    print(f"\nPolling [{label or task_id}]...")
    last_step = ""

    while True:
        data = _get(f"/task/{task_id}")
        status = data["status"]
        progress = data["progress"]
        step = data.get("current_step", "")
        task_type = data.get("task_type", "split")
        phase = data.get("pipeline_phase", "")

        if step != last_step:
            extra = f" [{phase}]" if phase else ""
            print(f"  [{progress:5.1f}%] ({task_type}{extra}) {step}")
            last_step = step

        if status == "completed":
            print(f"\n  Done — {task_type} task completed")
            return data
        if status == "failed":
            msg = data.get("error_message", "unknown error")
            print(f"\n  FAILED: {msg}")
            sys.exit(1)

        time.sleep(POLL_INTERVAL)


def save_json(data: dict, output_dir: Path, filename: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / filename
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  JSON → {path}")
    return path


def save_binary(content: bytes, output_dir: Path, filename: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / filename
    path.write_bytes(content)
    print(f"  File → {path}")
    return path


# ═══════════════════════════════════════════════════════════════════════════
# Split
# ═══════════════════════════════════════════════════════════════════════════

def run_split(file_path: str) -> str:
    """Upload document, poll until split complete, download results. Returns task_id."""
    if not Path(file_path).exists():
        print(f"File not found: {file_path}")
        sys.exit(1)

    fname = Path(file_path).name
    print(f"\n{'='*60}")
    print(f"Phase 1: Clause Splitting")
    print(f"  File: {file_path}")
    print(f"{'='*60}")

    # Upload
    print(f"\nUploading: {fname}")
    with open(file_path, "rb") as f:
        resp = _post("/split", files={"file": (fname, f)})
    task_id = resp["task_id"]
    print(f"  Task ID: {task_id}")

    # Poll
    final = poll_task(task_id, label=f"split:{fname}")
    doc_name = final.get("document_name", Path(file_path).stem)

    # Download results
    out_dir = OUTPUT_ROOT / Path(doc_name).stem
    print(f"\nDownloading results → {out_dir}/")

    json_data = _get(f"/result/{task_id}/json")
    save_json(json_data, out_dir, f"{Path(doc_name).stem}_split_result.json")

    try:
        excel_bytes = _get_raw(f"/result/{task_id}/excel")
        save_binary(excel_bytes, out_dir, f"{Path(doc_name).stem}_条款拆分结果.xlsx")
    except Exception:
        pass  # Excel may not exist for all document types

    # Summary
    _print_split_summary(json_data)

    print(f"\nSplit done. task_id={task_id}")
    return task_id


def _print_split_summary(data: dict) -> None:
    print(f"\n{'─'*50}")
    print(f"Document:    {data.get('document_name', '?')}")
    print(f"Articles:    {data.get('total_articles', 0)}")
    print(f"Obligations: {data.get('total_obligations', 0)}")
    print(f"Time:        {data.get('processing_time_seconds', 0):.1f}s")
    print(f"Model:       {data.get('model_used', 'N/A')}")

    obls = data.get("obligations", [])
    if obls:
        print(f"\n  First 3 obligations:")
        for o in obls[:3]:
            idx = o.get("index", "?")
            subj = o.get("subject", "?")
            verb = o.get("action_verb", "?")
            target = o.get("action_target", "?")
            clause = o.get("clause_number", "?")
            print(f"  [{idx}] {subj} → {verb}{target}  ({clause})")
    print(f"{'─'*50}")


# ═══════════════════════════════════════════════════════════════════════════
# Compare
# ═══════════════════════════════════════════════════════════════════════════

def run_compare(split_task_id: str, internal_file: str, internal_doc_name: str = "") -> str:
    """Upload internal obligations, compare against split result. Returns task_id."""
    if not Path(internal_file).exists():
        print(f"File not found: {internal_file}")
        sys.exit(1)

    int_name = internal_doc_name or Path(internal_file).stem
    print(f"\n{'='*60}")
    print(f"Phase 2: Clause Comparison")
    print(f"  Split task: {split_task_id}")
    print(f"  Internal:   {internal_file}")
    print(f"{'='*60}")

    # Upload internal file + split task ID
    print(f"\nSubmitting compare task...")
    with open(internal_file, "rb") as f:
        resp = _post(
            "/compare",
            data={
                "split_task_id": split_task_id,
                "internal_document_name": int_name,
            },
            files={"internal_file": (Path(internal_file).name, f, "application/json")},
        )
    task_id = resp["task_id"]
    print(f"  Compare task ID: {task_id}")

    # Poll
    final = poll_task(task_id, label=f"compare:{int_name}")

    # Download result
    out_dir = OUTPUT_ROOT / int_name
    json_data = _get(f"/result/{task_id}/json")
    save_json(json_data, out_dir, f"{int_name}_compare_result.json")

    # Print comparison summary
    _print_compare_summary(json_data)

    print(f"\nCompare done. task_id={task_id}")
    return task_id


def _print_compare_summary(data: dict) -> None:
    matches = data.get("matches", [])
    fully = sum(1 for m in matches if m.get("status") == "完全覆盖")
    partial = sum(1 for m in matches if m.get("status") == "部分覆盖")
    not_covered = sum(1 for m in matches if m.get("status") == "缺失")
    total = len(matches)

    print(f"\n{'─'*50}")
    print(f"External doc:   {data.get('external_document_name', '?')}")
    print(f"Internal doc:   {data.get('internal_document_name', '?')}")
    print(f"Total compared: {total}")
    print(f"  Fully covered:      {fully:3d} ({fully/max(total,1)*100:.0f}%)")
    print(f"  Partially covered:  {partial:3d} ({partial/max(total,1)*100:.0f}%)")
    print(f"  Not covered:        {not_covered:3d} ({not_covered/max(total,1)*100:.0f}%)")

    # Show gaps
    gaps = [m for m in matches if m.get("status") != "完全覆盖"]
    if gaps:
        print(f"\n  Gaps found ({len(gaps)}):")
        for g in gaps[:5]:
            ext_id = g.get("external_clause_id", "?")
            status = g.get("status", "?")
            analysis = g.get("analysis", "")[:100]
            print(f"  [{ext_id}] {status}: {analysis}...")
    print(f"{'─'*50}")


# ═══════════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════════

def run_summary(
    compare_task_id: str,
    external_raw_text: str = "",
    internal_raw_text: str = "",
    doc_name: str = "",
) -> str:
    """Generate gap analysis report from compare result. Returns task_id."""
    print(f"\n{'='*60}")
    print(f"Phase 3: Gap Analysis Report")
    print(f"  Compare task: {compare_task_id}")
    print(f"{'='*60}")

    print(f"\nSubmitting summary task...")
    resp = _post(
        "/summary",
        data={
            "compare_task_id": compare_task_id,
            "external_raw_text": external_raw_text,
            "internal_raw_text": internal_raw_text,
            "document_name": doc_name or "差距分析",
        },
    )
    task_id = resp["task_id"]
    print(f"  Summary task ID: {task_id}")

    # Poll
    final = poll_task(task_id, label=f"summary:{doc_name}")

    # Download result
    out_dir = OUTPUT_ROOT / (doc_name or "report")
    json_data = _get(f"/result/{task_id}/json")
    save_json(json_data, out_dir, "summary_result.json")

    # Download markdown report
    try:
        report_bytes = _get_raw(f"/result/{task_id}/report")
        save_binary(report_bytes, out_dir, "差距分析报告.md")
    except Exception as e:
        print(f"  (Report file not available: {e})")

    # Print excerpt
    report = json_data.get("report", json_data.get("summary_report", ""))
    if report:
        preview = report[:500]
        print(f"\n{'─'*50}")
        print(f"Report preview ({len(report)} chars total):")
        print(preview)
        if len(report) > 500:
            print("...")
        print(f"{'─'*50}")

    print(f"\nSummary done. task_id={task_id}")
    return task_id


# ═══════════════════════════════════════════════════════════════════════════
# Pipeline (split → compare → summary)
# ═══════════════════════════════════════════════════════════════════════════

def run_pipeline(
    file_path: str,
    internal_file: str,
    internal_raw_text: str = "",
    doc_name: str = "",
    internal_doc_name: str = "",
) -> None:
    """Run full pipeline: split → compare → summary."""
    if not Path(file_path).exists():
        print(f"File not found: {file_path}")
        sys.exit(1)
    if not Path(internal_file).exists():
        print(f"File not found: {internal_file}")
        sys.exit(1)

    fname = Path(file_path).name
    print(f"\n{'='*60}")
    print(f"FULL PIPELINE")
    print(f"  External:  {file_path}")
    print(f"  Internal:  {internal_file}")
    print(f"{'='*60}")

    # Submit pipeline
    print(f"\nSubmitting pipeline task...")
    with open(file_path, "rb") as ext_f, open(internal_file, "rb") as int_f:
        resp = _post(
            "/pipeline",
            data={
                "document_name": doc_name or fname,
                "internal_document_name": internal_doc_name or Path(internal_file).stem,
                "internal_raw_text": internal_raw_text,
            },
            files={
                "file": (fname, ext_f),
                "internal_file": (Path(internal_file).name, int_f, "application/json"),
            },
        )
    task_id = resp["task_id"]
    print(f"  Pipeline task ID: {task_id}")
    print(f"  Message: {resp['message']}")

    # Poll — this will show each phase
    final = poll_task(task_id, label=f"pipeline:{fname}")
    doc_name_final = final.get("document_name", Path(file_path).stem)

    # Download results
    out_dir = OUTPUT_ROOT / Path(doc_name_final).stem
    print(f"\nDownloading results → {out_dir}/")

    json_data = _get(f"/result/{task_id}/json")
    save_json(json_data, out_dir, f"{Path(doc_name_final).stem}_pipeline_result.json")

    try:
        excel_bytes = _get_raw(f"/result/{task_id}/excel")
        save_binary(excel_bytes, out_dir, f"{Path(doc_name_final).stem}_条款拆分结果.xlsx")
    except Exception:
        pass

    try:
        report_bytes = _get_raw(f"/result/{task_id}/report")
        save_binary(report_bytes, out_dir, f"{Path(doc_name_final).stem}_差距分析报告.md")
    except Exception:
        pass

    # Summary
    _print_pipeline_summary(json_data)
    print(f"\nPipeline done. All files saved to: {out_dir}")


def _print_pipeline_summary(data: dict) -> None:
    split = data.get("split_result", {})
    compare = data.get("compare_result", {})
    report = data.get("summary_report", "")

    print(f"\n{'─'*50}")
    print(f"Pipeline Results:")
    print(f"  Split:  {split.get('total_articles', 0)} articles → {split.get('total_obligations', 0)} obligations")

    matches = compare.get("matches", [])
    fully = sum(1 for m in matches if m.get("status") == "完全覆盖")
    not_cov = sum(1 for m in matches if m.get("status") == "缺失")
    print(f"  Compare: {len(matches)} compared — {fully} fully covered, {not_cov} not covered")

    print(f"  Report: {len(report)} chars")
    print(f"{'─'*50}")


# ═══════════════════════════════════════════════════════════════════════════
# List tasks
# ═══════════════════════════════════════════════════════════════════════════

def run_list(task_type: str = "") -> None:
    """List all recorded tasks."""
    url = f"/tasks"
    if task_type:
        url += f"?task_type={task_type}"

    data = _get(url)
    tasks = data.get("tasks", [])

    if not tasks:
        print("No tasks found. Run a split/compare/pipeline first.")
        return

    print(f"\n{'='*80}")
    print(f"{'ID':<14} {'Type':<10} {'Status':<12} {'Progress':>8} {'Document'}")
    print(f"{'─'*80}")

    status_icons = {
        "pending": "⏳",
        "parsing": "📄",
        "analyzing_structure": "🔍",
        "planning_batches": "📋",
        "decomposing": "🔧",
        "validating": "✅",
        "exporting": "📊",
        "completed": "✓",
        "failed": "✗",
    }

    for t in tasks:
        icon = status_icons.get(t.get("status", ""), " ")
        pid = t.get("task_id", "")
        ptype = t.get("task_type", "")
        pstatus = t.get("status", "")
        pprog = t.get("progress", 0)
        pdoc = t.get("document_name", "")[:36]
        perr = t.get("error_message", "")
        phase = t.get("pipeline_phase", "")

        extra = f" [{phase}]" if phase else ""
        print(f"{pid:<14} {ptype:<10} {icon} {pstatus:<10} {pprog:>7.0f}% {pdoc}{extra}")

        if perr:
            print(f"  {'↳':>14}  ✗ ERROR: {perr[:120]}")

    completed = [t for t in tasks if t.get("status") == "completed"]
    failed = [t for t in tasks if t.get("status") == "failed"]
    running = [t for t in tasks if t.get("status") not in ("completed", "failed")]

    print(f"{'─'*80}")
    print(f"Total: {len(tasks)}  |  Running: {len(running)}  |  Completed: {len(completed)}  |  Failed: {len(failed)}")
    print(f"{'='*80}\n")


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def main() -> None:
    global BASE_URL

    parser = argparse.ArgumentParser(
        description="Clause Splitter — End-to-End Test Client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python test_app.py --split --file 外部法规.docx
  python test_app.py --compare --split-task-id abc123 --internal-file 内规.json
  python test_app.py --summary --compare-task-id def456
  python test_app.py --pipeline --file 外部法规.docx --internal-file 内规.json
  python test_app.py --list          # show all tasks
  python test_app.py --list split    # show split tasks only
        """,
    )

    # Mode
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--split", action="store_true", help="Run clause splitting only")
    mode.add_argument("--compare", action="store_true", help="Run clause comparison only")
    mode.add_argument("--summary", action="store_true", help="Run gap analysis report generation only")
    mode.add_argument("--pipeline", action="store_true", help="Run full pipeline (split → compare → summary)")
    mode.add_argument("--list", nargs="?", const="", metavar="TYPE",
                      help="List all tasks (optionally filter by type: split/compare/summary/pipeline)")

    # Common
    parser.add_argument("--base-url", default=BASE_URL, help=f"API base URL (default: {BASE_URL})")
    parser.add_argument("--file", help="File path for split/pipeline (external regulation)")
    parser.add_argument("--document-name", default="", help="Override document name")

    # Compare
    parser.add_argument("--split-task-id", help="Completed split task ID (for --compare)")
    parser.add_argument("--internal-file", help="Pre-split internal obligations JSON file (for --compare / --pipeline)")
    parser.add_argument("--internal-doc-name", default="", help="Internal document name")

    # Summary
    parser.add_argument("--compare-task-id", help="Completed compare task ID (for --summary)")
    parser.add_argument("--external-text", default="", help="External regulation raw text for report context")
    parser.add_argument("--internal-text", default="", help="Internal regulation raw text for report context")

    args = parser.parse_args()

    # Override base URL from CLI if needed
    BASE_URL = args.base_url

    # Determine mode
    if not any([args.split, args.compare, args.summary, args.pipeline, args.list is not None]):
        # Default: split mode if --file given
        if args.file:
            args.split = True
        else:
            parser.print_help()
            print("\nTip: use --split --file <path> for quick split test, or --list to see tasks")
            sys.exit(1)

    print(f"Server: {BASE_URL}")

    if args.list is not None:
        run_list(task_type=args.list)

    elif args.split:
        if not args.file:
            print("Error: --file is required for --split mode")
            sys.exit(1)
        run_split(args.file)

    elif args.compare:
        if not args.split_task_id:
            print("Error: --split-task-id is required for --compare mode")
            sys.exit(1)
        if not args.internal_file:
            print("Error: --internal-file is required for --compare mode")
            sys.exit(1)
        run_compare(args.split_task_id, args.internal_file, args.internal_doc_name)

    elif args.summary:
        if not args.compare_task_id:
            print("Error: --compare-task-id is required for --summary mode")
            sys.exit(1)
        run_summary(
            args.compare_task_id,
            external_raw_text=args.external_text,
            internal_raw_text=args.internal_text,
            doc_name=args.document_name,
        )

    elif args.pipeline:
        if not args.file:
            print("Error: --file is required for --pipeline mode")
            sys.exit(1)
        if not args.internal_file:
            print("Error: --internal-file is required for --pipeline mode")
            sys.exit(1)
        run_pipeline(
            args.file,
            args.internal_file,
            internal_raw_text=args.internal_text,
            doc_name=args.document_name,
            internal_doc_name=args.internal_doc_name,
        )


if __name__ == "__main__":
    main()
