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

    # 5. List tasks
    python test_app.py --list
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

console = Console()

BASE_URL = os.environ.get("CLAUSE_SPLITTER_URL", "http://localhost:8000/api/v1")
OUTPUT_ROOT = Path(__file__).parent / "outputs"
POLL_INTERVAL = 1.5

# ── Icons ─────────────────────────────────────────────────────────────────

ICONS = {
    "split": "📝",
    "compare": "🔍",
    "summary": "📋",
    "pipeline": "🚀",
    "upload": "📤",
    "download": "📥",
    "file": "📄",
    "json": "📦",
    "excel": "📊",
    "report": "📰",
    "clock": "⏱️",
    "check": "✅",
    "cross": "❌",
    "warn": "⚠️",
    "bullet": "▪",
    "star": "⭐",
    "rocket": "🚀",
    "gear": "⚙️",
    "link": "🔗",
    "target": "🎯",
    "shield": "🛡️",
    "book": "📖",
    "search": "🔎",
    "stats": "📈",
    "folder": "📁",
}

STATUS_ICONS = {
    "pending": "⏳",
    "parsing": "📄",
    "analyzing_structure": "🔍",
    "planning_batches": "📋",
    "decomposing": "🔧",
    "validating": "✅",
    "exporting": "📊",
    "completed": "✅",
    "failed": "❌",
}

TYPE_ICONS = {"split": "📝", "compare": "🔍", "summary": "📋", "pipeline": "🚀"}
TYPE_COLORS = {"split": "cyan", "compare": "yellow", "summary": "magenta", "pipeline": "green"}
FILE_ICONS = {".pdf": "📕", ".docx": "📘", ".doc": "📘", ".txt": "📄", ".json": "📦", ".xlsx": "📊"}


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


def _file_icon(path: str) -> str:
    return FILE_ICONS.get(Path(path).suffix.lower(), "📄")


def _banner(icon: str, title: str, subtitle: str = "", color: str = "cyan") -> None:
    """Print a styled banner for a pipeline phase."""
    console.print()
    console.print(Rule(
        f"{icon}  [bold {color}]{title}[/]  {icon}",
        style=color, characters="─",
    ))
    if subtitle:
        console.print(Text(f"  {subtitle}", style="dim"))


def poll_task(task_id: str, label: str = "") -> dict:
    """Poll task status with live-updating progress bar. Returns final status JSON."""
    task_type = "?"
    type_icon = "?"

    progress = Progress(
        SpinnerColumn(spinner_name="dots"),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=40, complete_style="green", finished_style="bright_green"),
        TextColumn("[progress.percentage]{task.percentage:>4.0f}%"),
        TimeElapsedColumn(),
        console=console,
    )
    step_text = Text("🔌 connecting...", style="dim")
    task_bar = progress.add_task(f"[{label or task_id}]", total=100)

    def _layout() -> Panel:
        t = Table.grid(padding=(0, 2))
        t.add_row(progress)
        t.add_row(Text("  ", style="dim"))
        t.add_row(step_text)
        icon_display = TYPE_ICONS.get(task_type, "●")
        return Panel(t, title=f"{icon_display}  {task_type.upper()}",
                    border_style="blue", subtitle=task_id)

    with Live(_layout(), console=console, refresh_per_second=4,
              transient=False, vertical_overflow="visible") as live:
        while True:
            data = _get(f"/task/{task_id}")
            status = data["status"]
            pct = data.get("progress", 0)
            step = data.get("current_step", "")
            task_type = data.get("task_type", "split")
            phase = data.get("pipeline_phase", "")
            err = data.get("error_message", "")

            progress.update(task_bar, completed=pct,
                          description=f"[{label or task_id[:8]}]")

            status_icon = STATUS_ICONS.get(status, "●")

            if status == "completed":
                step_text = Text(f"{ICONS['check']}  ", style="green bold") + Text(step, style="green")
                progress.update(task_bar, completed=100)
                live.update(_layout())
                console.print()
                console.print(Panel(
                    Text(f"✅  {task_type.upper()} completed successfully", style="green bold"),
                    border_style="green",
                ))
                return data

            if status == "failed":
                step_text = Text(f"{ICONS['cross']}  ", style="red bold") + Text(step, style="red")
                if err:
                    step_text += Text(f"\n    {err[:200]}", style="red dim")
                live.update(_layout())
                console.print()
                console.print(Panel(Text(err or "unknown error", style="red"),
                                    title="[red bold]❌  FAILED[/]", border_style="red"))
                sys.exit(1)

            step_text = Text(f"{status_icon}  ", style="bold") + Text(step, style="white")
            if pct > 0:
                step_text += Text(f"  ({pct:.0f}%)", style="dim")
            if phase:
                phase_icon = TYPE_ICONS.get(phase, "●")
                step_text += Text(f"  {phase_icon} {phase}", style="italic dim")

            live.update(_layout())
            time.sleep(POLL_INTERVAL)


def save_json(data: dict, output_dir: Path, filename: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / filename
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"  {ICONS['json']}  JSON → [dim]{path}[/]")
    return path


def save_binary(content: bytes, output_dir: Path, filename: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / filename
    path.write_bytes(content)
    icon = ICONS.get("excel") if filename.endswith(".xlsx") else ICONS.get("report") if filename.endswith(".md") else ICONS["file"]
    console.print(f"  {icon}  File → [dim]{path}[/]")
    return path


# ═══════════════════════════════════════════════════════════════════════════
# Split
# ═══════════════════════════════════════════════════════════════════════════

def run_split(file_path: str) -> str:
    """Upload document, poll until split complete, download results."""
    if not Path(file_path).exists():
        console.print(f"\n  {ICONS['cross']} [red]File not found:[/] {file_path}")
        sys.exit(1)

    fname = Path(file_path).name
    ficon = _file_icon(file_path)

    _banner(ICONS["split"], "Phase 1 — Clause Splitting",
            f"{ficon}  {Path(file_path).absolute()}", color="cyan")

    # Upload
    console.print(f"\n  {ICONS['upload']}  [bold]Uploading:[/] [cyan]{fname}[/] ...")
    with open(file_path, "rb") as f:
        resp = _post("/split", files={"file": (fname, f)})
    task_id = resp["task_id"]
    console.print(f"  {ICONS['check']}  Task created: [bold cyan]{task_id}[/]")

    # Poll
    final = poll_task(task_id, label=f"split:{fname}")
    doc_name = final.get("document_name", Path(file_path).stem)

    # Download results
    out_dir = OUTPUT_ROOT / Path(doc_name).stem
    console.print(f"\n  {ICONS['download']}  [bold]Saving results →[/] [dim]{out_dir}/[/]")

    json_data = _get(f"/result/{task_id}/json")
    save_json(json_data, out_dir, f"{Path(doc_name).stem}_split_result.json")

    try:
        excel_bytes = _get_raw(f"/result/{task_id}/excel")
        save_binary(excel_bytes, out_dir, f"{Path(doc_name).stem}_条款拆分结果.xlsx")
    except Exception:
        pass

    _print_split_summary(json_data)

    console.print(f"\n  {ICONS['check']}  [green bold]Split done.[/] [dim]task_id={task_id}[/]")
    return task_id


def _print_split_summary(data: dict) -> None:
    obls = data.get("obligations", [])
    table = Table(title=f"{ICONS['stats']}  Split Result", border_style="cyan",
                  title_style="bold cyan")
    table.add_column("", style="cyan", width=2)
    table.add_column("Metric", style="white")
    table.add_column("Value", style="bold")

    table.add_row("📄", "Document", data.get('document_name', '?'))
    table.add_row("📑", "Articles", f"[cyan]{data.get('total_articles', 0)}[/]")
    table.add_row("🎯", "Obligations", f"[green bold]{data.get('total_obligations', 0)}[/]")
    table.add_row("⏱️", "Processing Time", f"{data.get('processing_time_seconds', 0):.1f}s")
    table.add_row("🤖", "Model", data.get('model_used', 'N/A'))

    console.print()
    console.print(table)

    if obls:
        console.print(f"\n  {ICONS['bullet']}  [bold]Sample obligations:[/]")
        for o in obls[:3]:
            idx = o.get("index", "?")
            subj = o.get("subject", "?")
            verb = o.get("action_verb", "?")
            target = o.get("action_target", "?")
            clause = o.get("clause_number", "?")
            console.print(
                f"  [dim]#{idx}[/] "
                f"[cyan]{subj}[/] {ICONS['bullet']} "
                f"[yellow bold]{verb}[/][white]{target}[/]  "
                f"[dim]← {clause}[/]"
            )


# ═══════════════════════════════════════════════════════════════════════════
# Compare
# ═══════════════════════════════════════════════════════════════════════════

def run_compare(split_task_id: str, internal_file: str, internal_doc_name: str = "") -> str:
    """Upload internal obligations, compare against split result."""
    if not Path(internal_file).exists():
        console.print(f"\n  {ICONS['cross']} [red]File not found:[/] {internal_file}")
        sys.exit(1)

    int_name = internal_doc_name or Path(internal_file).stem
    ficon = _file_icon(internal_file)

    _banner(ICONS["compare"], "Phase 2 — Clause Comparison",
            f"{ICONS['link']}  split_task={split_task_id}  |  {ficon}  internal={Path(internal_file).name}",
            color="yellow")

    console.print(f"\n  {ICONS['upload']}  [bold]Submitting compare task...[/]")
    with open(internal_file, "rb") as f:
        resp = _post(
            "/compare",
            data={"split_task_id": split_task_id, "internal_document_name": int_name},
            files={"internal_file": (Path(internal_file).name, f, "application/json")},
        )
    task_id = resp["task_id"]
    console.print(f"  {ICONS['check']}  Compare task: [bold yellow]{task_id}[/]")

    final = poll_task(task_id, label=f"compare:{int_name}")

    out_dir = OUTPUT_ROOT / int_name
    json_data = _get(f"/result/{task_id}/json")
    save_json(json_data, out_dir, f"{int_name}_compare_result.json")

    _print_compare_summary(json_data)

    console.print(f"\n  {ICONS['check']}  [green bold]Compare done.[/] [dim]task_id={task_id}[/]")
    return task_id


def _print_compare_summary(data: dict) -> None:
    matches = data.get("matches", [])
    fully = sum(1 for m in matches if m.get("status") == "完全覆盖")
    partial = sum(1 for m in matches if m.get("status") == "部分覆盖")
    not_covered = sum(1 for m in matches if m.get("status") == "缺失")
    total = len(matches)

    table = Table(title=f"{ICONS['search']}  Compare Result", border_style="yellow",
                  title_style="bold yellow")
    table.add_column("", width=2)
    table.add_column("Metric", style="white")
    table.add_column("Value", style="bold")

    table.add_row("📕", "External", data.get('external_document_name', '?'))
    table.add_row("📘", "Internal", data.get('internal_document_name', '?'))
    table.add_row("📊", "Total Compared", str(total))

    fr = f"[green bold]{fully}[/] ({fully/max(total,1)*100:.0f}%)"
    pr = f"[yellow bold]{partial}[/] ({partial/max(total,1)*100:.0f}%)"
    nr = f"[red bold]{not_covered}[/] ({not_covered/max(total,1)*100:.0f}%)"
    table.add_row("🟢", "Fully Covered", fr)
    table.add_row("🟡", "Partially Covered", pr)
    table.add_row("🔴", "Not Covered", nr)

    # Coverage bar
    bar_width = 30
    fw = int(fully / max(total, 1) * bar_width)
    pw = int(partial / max(total, 1) * bar_width)
    nw = max(bar_width - fw - pw, 0)
    bar = f"[green]{'█'*fw}[/][yellow]{'█'*pw}[/][red]{'█'*nw}[/]"
    table.add_row("📈", "Coverage", bar)

    console.print()
    console.print(table)

    gaps = [m for m in matches if m.get("status") != "完全覆盖"]
    if gaps:
        console.print(f"\n  {ICONS['warn']}  [bold red]{len(gaps)} gaps found:[/]")
        for g in gaps[:5]:
            ext_id = g.get("external_clause_id", "?")
            status = g.get("status", "?")
            sc = {"部分覆盖": "yellow", "缺失": "red"}.get(status, "white")
            analysis = g.get("analysis", "")[:120]
            icon = {"部分覆盖": "🟡", "缺失": "🔴"}.get(status, "⚪")
            console.print(f"  [dim]#{ext_id}[/] {icon} [{sc}]{status}[/]: {analysis}...")
    else:
        console.print(f"\n  {ICONS['star']}  [green bold]All obligations fully covered![/]")


# ═══════════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════════

def run_summary(
    compare_task_id: str,
    external_raw_text: str = "",
    internal_raw_text: str = "",
    doc_name: str = "",
) -> str:
    """Generate gap analysis report from compare result."""
    _banner(ICONS["summary"], "Phase 3 — Gap Analysis Report",
            f"{ICONS['link']}  compare_task={compare_task_id}", color="magenta")

    console.print(f"\n  {ICONS['gear']}  [bold]Generating report...[/]")
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
    console.print(f"  {ICONS['check']}  Summary task: [bold magenta]{task_id}[/]")

    final = poll_task(task_id, label=f"summary:{doc_name}")

    out_dir = OUTPUT_ROOT / (doc_name or "report")
    json_data = _get(f"/result/{task_id}/json")
    save_json(json_data, out_dir, "summary_result.json")

    try:
        report_bytes = _get_raw(f"/result/{task_id}/report")
        save_binary(report_bytes, out_dir, "差距分析报告.md")
    except Exception as e:
        console.print(f"  {ICONS['warn']}  [dim]Report file not available: {e}[/]")

    report = json_data.get("report", json_data.get("summary_report", ""))
    if report:
        preview = report[:400]
        console.print()
        console.print(Panel(
            Text(preview + ("..." if len(report) > 400 else ""), style="white"),
            title=f"{ICONS['report']}  Report Preview ({len(report)} chars)",
            border_style="magenta",
        ))

    console.print(f"\n  {ICONS['check']}  [green bold]Summary done.[/] [dim]task_id={task_id}[/]")
    return task_id


# ═══════════════════════════════════════════════════════════════════════════
# Pipeline
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
        console.print(f"\n  {ICONS['cross']} [red]File not found:[/] {file_path}")
        sys.exit(1)
    if not Path(internal_file).exists():
        console.print(f"\n  {ICONS['cross']} [red]File not found:[/] {internal_file}")
        sys.exit(1)

    fname = Path(file_path).name
    fext_icon = _file_icon(file_path)
    fint_icon = _file_icon(internal_file)

    _banner(ICONS["pipeline"], "FULL PIPELINE  —  Split → Compare → Report",
            f"{fext_icon}  external={Path(file_path).name}  |  {fint_icon}  internal={Path(internal_file).name}",
            color="green")

    console.print(f"\n  {ICONS['rocket']}  [bold]Launching pipeline...[/]")
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
    console.print(f"  {ICONS['check']}  Pipeline task: [bold green]{task_id}[/]")
    console.print(f"  {ICONS['bullet']}  [dim]{resp['message']}[/]")

    final = poll_task(task_id, label=f"pipeline:{fname}")
    doc_name_final = final.get("document_name", Path(file_path).stem)

    out_dir = OUTPUT_ROOT / Path(doc_name_final).stem
    console.print(f"\n  {ICONS['download']}  [bold]Saving results →[/] [dim]{out_dir}/[/]")

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

    _print_pipeline_summary(json_data)
    console.print(f"\n  {ICONS['star']}  [green bold]Pipeline complete![/] [dim]All files → {out_dir}[/]")


def _print_pipeline_summary(data: dict) -> None:
    split = data.get("split_result", {})
    compare = data.get("compare_result", {})
    report = data.get("summary_report", "")

    matches = compare.get("matches", [])
    fully = sum(1 for m in matches if m.get("status") == "完全覆盖")
    not_cov = sum(1 for m in matches if m.get("status") == "缺失")

    table = Table(title=f"{ICONS['rocket']}  Pipeline Results", border_style="green",
                  title_style="bold green")
    table.add_column("", width=2)
    table.add_column("Phase", style="white")
    table.add_column("Result", style="bold")

    table.add_row("📝", "Split",
                  f"{split.get('total_articles', 0)} articles → [green]{split.get('total_obligations', 0)}[/] obligations")
    table.add_row("🔍", "Compare",
                  f"{len(matches)} matched — [green]{fully} covered[/], [red]{not_cov} missing[/]")
    table.add_row("📋", "Report",
                  f"[cyan]{len(report):,}[/] chars generated")

    console.print()
    console.print(table)


# ═══════════════════════════════════════════════════════════════════════════
# List tasks
# ═══════════════════════════════════════════════════════════════════════════

def run_list(task_type: str = "") -> None:
    """List all recorded tasks with rich table."""
    url = "/tasks"
    if task_type:
        url += f"?task_type={task_type}"

    data = _get(url)
    tasks = data.get("tasks", [])

    if not tasks:
        console.print(f"\n  {ICONS['folder']}  [dim]No tasks yet. Run --split or --pipeline first.[/]\n")
        return

    table = Table(title=f"{ICONS['folder']}  All Tasks", border_style="blue",
                  header_style="bold cyan", title_style="bold")
    table.add_column("ID", style="dim", width=14)
    table.add_column("Type", width=10)
    table.add_column("Status", width=26)
    table.add_column("Progress", justify="center", width=10)
    table.add_column("Document")

    completed_c = 0
    failed_c = 0
    running_c = 0

    for t in tasks:
        pid = t.get("task_id", "")
        ptype = t.get("task_type", "")
        pstatus = t.get("status", "")
        pprog = t.get("progress", 0)
        pdoc = t.get("document_name", "")[:40]
        perr = t.get("error_message", "")
        phase = t.get("pipeline_phase", "")

        sicon = STATUS_ICONS.get(pstatus, "●")
        ticon = TYPE_ICONS.get(ptype, "●")
        tcolor = TYPE_COLORS.get(ptype, "white")

        # Mini progress bar
        bw = 6
        filled = int(pprog / 100 * bw)
        bar = "█" * filled + "░" * (bw - filled)

        status_line = f"{sicon} {pstatus}"
        if phase:
            status_line += f" [dim]› {phase}[/]"
        if perr:
            status_line += f"\n  [red dim]{perr[:80]}[/]"

        table.add_row(
            pid,
            f"[{tcolor}]{ticon} {ptype}[/]",
            status_line,
            f"{bar} {pprog:.0f}%",
            pdoc,
        )

        if pstatus == "completed":
            completed_c += 1
        elif pstatus == "failed":
            failed_c += 1
        else:
            running_c += 1

    console.print()
    console.print(table)
    console.print(
        f"  📊 Total: [bold]{len(tasks)}[/]  |  "
        f"🔄 Running: [yellow]{running_c}[/]  |  "
        f"✅ Completed: [green]{completed_c}[/]  |  "
        f"❌ Failed: [red]{failed_c}[/]\n"
    )


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def main() -> None:
    global BASE_URL

    parser = argparse.ArgumentParser(
        description=f"{ICONS['rocket']}  Clause Splitter — End-to-End Test Client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python test_app.py --split --file 外部法规.docx
  python test_app.py --compare --split-task-id abc123 --internal-file 内规.json
  python test_app.py --summary --compare-task-id def456
  python test_app.py --pipeline --file 外部法规.docx --internal-file 内规.json
  python test_app.py --list          # show all tasks
  python test_app.py --list split    # filter by type
        """,
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--split", action="store_true", help="📝  Clause splitting only")
    mode.add_argument("--compare", action="store_true", help="🔍  Clause comparison only")
    mode.add_argument("--summary", action="store_true", help="📋  Gap analysis report only")
    mode.add_argument("--pipeline", action="store_true", help="🚀  Full pipeline (split → compare → report)")
    mode.add_argument("--list", nargs="?", const="", metavar="TYPE",
                      help="📋  List all tasks (optional: filter by type)")
    mode.add_argument("--clear", action="store_true", help="🗑️   Clear all task records")

    parser.add_argument("--base-url", default=BASE_URL, help=f"API base URL (default: {BASE_URL})")
    parser.add_argument("--file", help="📄  External regulation file (for --split / --pipeline)")
    parser.add_argument("--document-name", default="", help="Override document name")

    parser.add_argument("--split-task-id", help="🔗  Split task ID (for --compare)")
    parser.add_argument("--internal-file", help="📦  Internal obligations JSON (for --compare / --pipeline)")
    parser.add_argument("--internal-doc-name", default="", help="Internal document name")

    parser.add_argument("--compare-task-id", help="🔗  Compare task ID (for --summary)")
    parser.add_argument("--external-text", default="", help="External regulation raw text")
    parser.add_argument("--internal-text", default="", help="Internal regulation raw text")

    args = parser.parse_args()
    BASE_URL = args.base_url

    if args.clear:
        resp = requests.delete(f"{BASE_URL}/tasks")
        resp.raise_for_status()
        data = resp.json()
        console.print(f"\n  🗑️  {data['message']}")
        return

    if not any([args.split, args.compare, args.summary, args.pipeline, args.list is not None]):
        if args.file:
            args.split = True
        else:
            parser.print_help()
            console.print(f"\n  💡 [dim]Tip: use --split --file <path> for a quick test, or --list to see tasks[/]")
            sys.exit(1)

    console.print(f"\n  🌐 Server: [bold cyan]{BASE_URL}[/]")

    if args.list is not None:
        run_list(task_type=args.list)
    elif args.split:
        if not args.file:
            console.print(f"\n  ❌ [red]--file is required for --split[/]")
            sys.exit(1)
        run_split(args.file)
    elif args.compare:
        if not args.split_task_id or not args.internal_file:
            console.print(f"\n  ❌ [red]--split-task-id and --internal-file are required for --compare[/]")
            sys.exit(1)
        run_compare(args.split_task_id, args.internal_file, args.internal_doc_name)
    elif args.summary:
        if not args.compare_task_id:
            console.print(f"\n  ❌ [red]--compare-task-id is required for --summary[/]")
            sys.exit(1)
        run_summary(args.compare_task_id, args.external_text, args.internal_text, args.document_name)
    elif args.pipeline:
        if not args.file or not args.internal_file:
            console.print(f"\n  ❌ [red]--file and --internal-file are required for --pipeline[/]")
            sys.exit(1)
        run_pipeline(args.file, args.internal_file, args.internal_text,
                    args.document_name, args.internal_doc_name)


if __name__ == "__main__":
    main()
