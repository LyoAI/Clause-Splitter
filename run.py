#!/usr/bin/env python3
"""End-to-end test script for Clause Splitter API.

Usage:
    # Start the server first:
    uv run main.py

    # ── Offline: Split + Index internal regulations (once) ──
    python run.py --pre-split --file 内规库.xlsx --output-dir outputs/internal
    python run.py --build-index --internal-dir outputs/internal/

    # ── Online: Compare external vs internal (every new external doc) ──
    python run.py --pipeline --file 外规.docx --internal-dir outputs/internal/ --output-dir outputs/external

    # ── Index management ──
    python run.py --index-info --internal-dir outputs/internal/
    python run.py --build-index --internal-dir outputs/internal/ --force   # rebuild

    # ── Other modes ──
    python run.py --split --file document.pdf
    python run.py --summary-comparison --external-files 外规.json --internal-files 内规.json
    python run.py --chain --file 外规.docx --internal-files 内规.json
    python run.py --list
"""

from __future__ import annotations

import argparse
import json
import os
import re
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


def _clean_doc_name(path: str) -> str:
    """Strip known suffixes from a filename stem to get the base document name.

    '网络数据安全风险评估办法_split_result.json' → '网络数据安全风险评估办法'
    """
    name = Path(path).stem
    for suffix in ("_split_result", "_compare_result", "_comparison_result",
                   "_pipeline_result", "_summary_result"):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    return name


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

def run_split(file_path: str, output_dir: str = "") -> str:
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
    base = Path(output_dir) if output_dir else OUTPUT_ROOT
    out_dir = base / Path(doc_name).stem
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
    table.add_column("", style="cyan", width=3, no_wrap=True)
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
def _print_compare_summary(data: dict) -> None:
    matches = data.get("matches", [])
    fully = sum(1 for m in matches if m.get("status") == "完全覆盖")
    partial = sum(1 for m in matches if m.get("status") == "部分覆盖")
    not_covered = sum(1 for m in matches if m.get("status") == "缺失")
    total = len(matches)

    table = Table(title=f"{ICONS['search']}  Compare Result", border_style="yellow",
                  title_style="bold yellow")
    table.add_column("", width=3, no_wrap=True)
    table.add_column("Metric", style="white")
    table.add_column("Value", style="bold")

    ext_docs = data.get('external_documents') or [data.get('external_document_name', '?')]
    int_docs = data.get('internal_documents') or [data.get('internal_document_name', '?')]
    table.add_row("📕", "External", ", ".join(ext_docs) if isinstance(ext_docs, list) else str(ext_docs))
    table.add_row("📘", "Internal", ", ".join(int_docs) if isinstance(int_docs, list) else str(int_docs))
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
# Pre-Split (pandas split large Excel by document name)
# ═══════════════════════════════════════════════════════════════════════════

# ── LLM column detection — one call, plain text output, no JSON ────────


def _llm_detect_columns(df) -> tuple[str, str, str]:
    """LLM 看一眼表头，告诉我们哪列是条款编号、哪列是条款内容、哪列是制度名称。

    只发送列名列表，LLM 用自然语言回复，我们解析一下就行。
    返回 (article_id_col, article_text_col, doc_name_col)。
    """
    col_list = "\n".join(f"  - {str(col)}" for col in df.columns)

    system_prompt = (
        "你是一个 Excel 列名识别助手。"
        "给定一个 Excel 的列名列表，请识别：\n"
        "1. 「条款内容」列 — 包含每一条的具体法律条文正文，逐条不同，不是整部法规的全文\n"
        "2. 「条款编号」列 — 条款的编号（如第一条、第十四条、1.1 等）\n"
        "3. 「制度名称」列 — 法规或制度的名称，在同一制度的多行中重复出现\n\n"
        "⚠️ 注意区分：「制度内容」或「法规内容」通常是整部法规的全文概要，内容极长且每行相同，"
        "这不是条款内容列。条款内容列每行是一条独立的条款。\n\n"
        "请用以下格式回复（不要输出其他内容，一行一个）：\n"
        "条款内容列: <列名>\n"
        "条款编号列: <列名>\n"
        "制度名称列: <列名>\n"
        "\n如果某一列不存在，对应的值写「无」。"
    )

    try:
        from openai import OpenAI
    except ImportError:
        return _detect_columns_by_heuristic(df)

    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        return _detect_columns_by_heuristic(df)

    try:
        client = OpenAI(
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key=api_key,
        )
        response = client.chat.completions.create(
            model=os.environ.get("LLM_MODEL", "qwen3-max"),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"列名列表：\n\n{col_list}"},
            ],
            temperature=0.1,
        )
        content = response.choices[0].message.content
    except Exception as e:
        console.print(f"  ⚠️  [yellow]LLM 调用失败: {e}，使用本地规则兜底[/]")
        return _detect_columns_by_heuristic(df)

    # ── 解析 LLM 回复：一行一个字段 ─────────────────────────────────
    available = set(str(c) for c in df.columns)
    id_col, text_col, doc_col = "", "", ""

    for line in content.split("\n"):
        line = line.strip()
        if "条款内容列" in line:
            text_col = line.split(":", 1)[-1].strip() if ":" in line else line.split("：", 1)[-1].strip()
            if text_col == "无":
                text_col = ""
        elif "条款编号列" in line:
            id_col = line.split(":", 1)[-1].strip() if ":" in line else line.split("：", 1)[-1].strip()
            if id_col == "无":
                id_col = ""
        elif "制度名称列" in line:
            doc_col = line.split(":", 1)[-1].strip() if ":" in line else line.split("：", 1)[-1].strip()
            if doc_col == "无":
                doc_col = ""

    # 验证列名是否真实存在于 DataFrame 中
    text_col = text_col if text_col in available else ""
    id_col = id_col if id_col in available else ""
    doc_col = doc_col if doc_col in available else ""

    # 模糊匹配兜底
    if not text_col:
        for col in available:
            # "制度内容"/"法规内容"是全量文本，不是逐条的条款内容
            if any(kw in col for kw in ["制度内容", "法规内容", "全文"]):
                continue
            if "条款" in col or "条文" in col or ("内容" in col and "制度" not in col and "法规" not in col):
                text_col = col
                break
    if not doc_col:
        for col in available:
            if "名称" in col or "制度" in col:
                doc_col = col
                break
    if not id_col:
        for col in available:
            if ("编号" in col or "条目" in col) and col != text_col:
                id_col = col
                break

    if text_col and doc_col:
        console.print(f"  🤖  LLM 识别: 条款内容=[cyan]{text_col}[/], "
                      f"条款编号=[cyan]{id_col or '(无)'}[/], "
                      f"制度名称=[cyan]{doc_col}[/]")
        return id_col, text_col, doc_col

    # LLM 回复解析失败，走本地规则
    console.print("  ⚠️  [yellow]LLM 回复解析失败，使用本地规则兜底[/]")
    return _detect_columns_by_heuristic(df)


def _detect_columns_by_heuristic(df) -> tuple[str, str, str]:
    """本地规则兜底：列名关键词 + 内容长度判断。"""
    import pandas as pd
    available = [str(c) for c in df.columns]

    # 制度名称列
    doc_col = ""
    for col in available:
        if any(kw in col for kw in ["制度名称", "法规名称", "文档名称", "文件名称"]):
            doc_col = col
            break
    if not doc_col:
        for col in available:
            if "名称" in col:
                doc_col = col
                break

    # 条款编号列
    id_col = ""
    for col in available:
        if any(kw in col for kw in ["条款编号", "条款名称", "条目名称"]):
            if col != doc_col:
                id_col = col
                break

    # 条款内容列
    # ⚠️ 注意区分：「制度内容」是全量文本（每行重复），「条款内容」才是每条的具体条文
    text_col = ""
    for col in available:
        if any(kw in col for kw in ["条款内容", "法规条款", "条文内容", "条款正文"]):
            text_col = col
            break
    if not text_col:
        # 宽松匹配：包含「条款」但不是制度全文列
        for col in available:
            if "条款" in col and "制度" not in col and col != doc_col and col != id_col:
                text_col = col
                break
    if not text_col:
        best_avg = 0
        for col in available:
            if df[col].dtype == "object" and col != doc_col and col != id_col:
                # 排除「制度内容」「法规内容」等全文列
                if any(kw in col for kw in ["制度内容", "法规内容", "全文"]):
                    continue
                avg = df[col].dropna().apply(lambda x: len(str(x))).mean()
                if avg > best_avg and avg > 20:
                    best_avg = avg
                    text_col = col

    console.print(f"  🔧  本地规则: 条款内容=[cyan]{text_col}[/], "
                  f"条款编号=[cyan]{id_col or '(无)'}[/], "
                  f"制度名称=[cyan]{doc_col}[/]")
    return id_col, text_col, doc_col


def run_pre_split(file_path: str, output_dir: str = "", force: bool = False) -> None:
    """Pre-split a large multi-document Excel into per-document .txt files, then split each.

    Instead of saving each group as .xlsx (which triggers the slow Excel pipeline with
    ReAct schema detection per sub-file), we extract the article text directly from the
    DataFrame and save as .txt — so each sub-document goes through the SAME fast text
    pipeline as a PDF would (parse_document → analyze_structure → decompose → export).

    If force=False, documents whose *_split_result.json already exists under output_dir
    will be skipped (resume support).
    """
    # Resolve output base for skip check and result saving
    _out_base = Path(output_dir) if output_dir else OUTPUT_ROOT
    import tempfile
    try:
        import pandas as pd
    except ImportError:
        console.print(f"\n  ❌ [red]pandas is required for --pre-split. Install: pip install pandas openpyxl[/]")
        sys.exit(1)

    if not Path(file_path).exists():
        console.print(f"\n  ❌ [red]File not found:[/] {file_path}")
        sys.exit(1)

    _banner(ICONS["gear"], "PRE-SPLIT — 大 Excel 按制度名称拆分 → .txt → 文本管道",
            f"📄  {Path(file_path).name}", color="blue")

    # ── Step 1: Read + LLM detect columns ONCE ──────────────────────
    console.print(f"\n  📖  Reading Excel with pandas...")
    try:
        df = pd.read_excel(file_path)
    except Exception as e:
        console.print(f"\n  ❌ [red]Failed to read Excel: {e}[/]")
        sys.exit(1)

    console.print(f"  📊  {len(df)} rows × {len(df.columns)} columns")
    console.print(f"  📋  Columns: {', '.join(str(c) for c in df.columns[:10])}")

    # One LLM call: look at headers, tell us which column is what
    console.print(f"\n  🤖  LLM 识别表头...")
    id_col, text_col, doc_col = _llm_detect_columns(df)

    if not doc_col or not text_col:
        console.print(f"\n  ❌ [red]无法识别所需列。"
                      f"制度名称='{doc_col}', 条款内容='{text_col}'[/]")
        console.print(f"  💡  Available columns: {list(df.columns)}")
        sys.exit(1)

    # 安全检查：条款内容列不应是"制度内容"这种全文列
    avg_len = df[text_col].dropna().apply(lambda x: len(str(x))).mean()
    if avg_len > 1000:
        console.print(f"  ⚠️  [yellow]警告: 条款内容列 '{text_col}' 平均每行 {avg_len:.0f} 字符[/]")
        console.print(f"  ⚠️  [yellow]这看起来像是「制度内容」全文列而非「条款内容」逐条列[/]")
        console.print(f"  💡  请检查 Excel 表头，或手动指定列名")
        # 尝试找一个更好的列
        for col in df.columns:
            col_str = str(col)
            if "条款" in col_str and "内容" in col_str and "制度" not in col_str:
                if col_str != text_col:
                    console.print(f"  🔧  自动切换为: [cyan]{col_str}[/]")
                    text_col = col_str
                    break

    # ── Step 2: Group → build structured .txt per document ────────────
    use_dir = Path(output_dir) if output_dir else Path(tempfile.mkdtemp(prefix="pre_split_"))
    use_dir.mkdir(parents=True, exist_ok=True)

    groups = df.groupby(doc_col)
    doc_names = list(groups.groups.keys())
    console.print(f"\n  📑  Found [bold cyan]{len(doc_names)}[/] documents via column '[cyan]{doc_col}[/]':")
    for name in doc_names[:20]:
        count = len(groups.get_group(name))
        console.print(f"      - {str(name)[:60]}  ({count} rows)")
    if len(doc_names) > 20:
        console.print(f"      ... and {len(doc_names) - 20} more")

    # Build .txt for each group
    sub_files: list[tuple[str, str]] = []  # (doc_name, txt_file_path)
    skipped = 0
    for doc_name, group_df in groups:
        safe_name = re.sub(r"[\\/:*?\"<>|]", "_", str(doc_name))[:80]

        # ── Skip if already processed (unless --force) ──────────────
        result_json = _out_base / safe_name / f"{safe_name}_split_result.json"
        if not force and result_json.exists():
            console.print(f"  ⏭️   [dim]Skip (already exists):[/] {doc_name[:60]}")
            skipped += 1
            continue

        txt_path = use_dir / f"{safe_name}.txt"

        # Build structured text: one article per paragraph with ID prefix
        lines: list[str] = []
        empty_count = 0
        for _, row in group_df.iterrows():
            art_text = str(row[text_col]) if pd.notna(row[text_col]) else ""
            if not art_text.strip():
                empty_count += 1
                continue

            # Prepend article ID if available (helps analyze_structure regex)
            if id_col and pd.notna(row.get(id_col)):
                art_id = str(row[id_col]).strip()
                if art_id:
                    # If text already starts with the ID, don't duplicate
                    if art_text.startswith(art_id):
                        lines.append(art_text)
                    else:
                        lines.append(f"{art_id}  {art_text}")
                else:
                    lines.append(art_text)
            else:
                lines.append(art_text)
            lines.append("")  # blank line between articles

        text_content = "\n".join(lines).strip()
        if not text_content:
            total_rows = len(group_df)
            console.print(
                f"  ⚠️  [yellow]Skipping empty document:[/] {doc_name}")
            console.print(
                f"      [dim]text_col=[{text_col}], "
                f"total_rows={total_rows}, "
                f"empty_rows={empty_count}, "
                f"non_empty={total_rows - empty_count}[/]")
            # Print first row's available column values for debugging
            if total_rows > 0:
                first_row = group_df.iloc[0]
                sample = {str(c): str(first_row[c])[:80] if pd.notna(first_row[c]) else "NaN"
                          for c in group_df.columns[:6]}
                console.print(f"      [dim]first row sample: {sample}[/]")
            continue

        txt_path.write_text(text_content, encoding="utf-8")
        sub_files.append((str(doc_name), str(txt_path)))

    if skipped:
        console.print(f"  ⏭️   [dim]{skipped} already processed → skipped[/]")
    console.print(f"\n  💾  Saved {len(sub_files)} .txt files → [dim]{use_dir}[/]")

    # ── Step 3: Process each .txt through text pipeline (= PDF path) ──
    if not sub_files:
        console.print(f"\n  ✅ [green]All {skipped} document(s) already processed. Nothing to do.[/]\n")
        return

    console.print(f"\n  🚀  Processing [bold]{len(sub_files)}[/] documents (text pipeline, same as PDF)...\n")

    success, failed = 0, 0
    for i, (doc_name, txt_path) in enumerate(sub_files):
        console.print(f"  {'─' * 60}")
        console.print(f"  [{i + 1}/{len(sub_files)}] [bold]{doc_name[:60]}[/] [dim]({Path(txt_path).stat().st_size} bytes)[/]")
        try:
            run_split(txt_path, output_dir=output_dir)
            success += 1
        except SystemExit:
            failed += 1
            console.print(f"  ⚠️  [yellow]Skipped (see error above)[/]")

    # ── Summary ───────────────────────────────────────────────────────
    console.print(f"\n  {'─' * 60}")
    summary_parts = [f"✅ [bold green]{success}[/] succeeded"]
    if failed:
        summary_parts.append(f"[bold red]{failed}[/] failed")
    if skipped:
        summary_parts.append(f"[dim]{skipped} skipped[/]")
    console.print(f"\n  {', '.join(summary_parts)} out of {len(sub_files) + skipped} total")
    console.print(f"  📁  Temp .txt files kept at: [dim]{use_dir}[/]")
    console.print(f"  📁  Outputs under: [dim]{_out_base}[/]")


# ═══════════════════════════════════════════════════════════════════════════
# Build BM25 Index (offline, after splitting internal regulations)
# ═══════════════════════════════════════════════════════════════════════════

def run_build_index(internal_dir: str, force: bool = False) -> None:
    """Build persistent BM25 index from all *_split_result.json files.

    This is the **offline** step — run once after splitting internal regulations.
    Subsequent comparisons will reuse this index (millisecond open, no rebuild).

    By default, if the index already exists, only NEW documents (not yet in the
    index) are appended — existing documents are skipped. Use --force to rebuild
    the entire index from scratch.

    Index is stored at: <internal_dir>/.bm25_index/

    Args:
        internal_dir: Directory containing **/*_split_result.json files.
        force: If True, full rebuild (delete old index). If False, smart append.
    """
    from tools.search.index_manager import IndexManager

    scan_dir = Path(internal_dir)
    if not scan_dir.is_dir():
        console.print(f"\n  ❌ [red]Directory not found:[/] {internal_dir}")
        sys.exit(1)

    index_dir = str(scan_dir / ".bm25_index")
    index_exists = IndexManager.index_exists(index_dir)

    if force and index_exists:
        _banner("🔧", "REBUILD BM25 INDEX (--force)",
                f"📁  {scan_dir.absolute()}  →  [dim]{index_dir}[/] (overwrite)",
                color="yellow")
    elif index_exists:
        _banner("➕", "APPEND TO BM25 INDEX",
                f"📁  {scan_dir.absolute()}  →  [dim]{index_dir}[/] (new documents only)",
                color="blue")
    else:
        _banner("🔧", "BUILD BM25 INDEX (first time)",
                f"📁  {scan_dir.absolute()}  →  [dim]{index_dir}[/]",
                color="blue")

    # If force rebuild, remove old index
    if force and index_exists:
        import shutil
        shutil.rmtree(index_dir)
        console.print(f"\n  🗑️   Old index removed: [dim]{index_dir}[/]")
        index_exists = False

    # Scan for split results
    json_files = sorted(scan_dir.glob("**/*_split_result.json"))
    if not json_files:
        console.print(f"\n  ❌ [red]No *_split_result.json files found in:[/] {internal_dir}")
        console.print(f"  💡  Run [bold]--pre-split[/] or [bold]--split[/] first to generate split results.")
        sys.exit(1)

    # Show existing index info
    if index_exists and not force:
        info = IndexManager.get_index_info(index_dir)
        manifest = IndexManager._read_manifest(index_dir)
        existing_doc_count = len(manifest.get("documents", {}))
        console.print(f"\n  📊  Existing index: [bold cyan]{info['num_docs']}[/] obligations, "
                      f"[bold cyan]{existing_doc_count}[/] documents")
        console.print(f"      Size: [dim]{info['size_mb']} MB[/]")
    else:
        existing_doc_count = 0

    # Show files found
    console.print(f"\n  📑  Found [bold cyan]{len(json_files)}[/] split result files:")

    total_obls = 0
    for fpath in json_files[:20]:
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
            obls = data if isinstance(data, list) else data.get("obligations", [])
            count = len(obls)
            total_obls += count
            rel = fpath.relative_to(scan_dir)
            console.print(f"      {ICONS['json']}  [dim]{rel}[/]  →  [cyan]{count}[/] obligations")
        except Exception:
            console.print(f"      {ICONS['warn']}  [dim]{fpath.name}[/]  →  [red]parse error[/]")

    if len(json_files) > 20:
        console.print(f"      ... and {len(json_files) - 20} more")

    console.print(f"\n  📊  Total in directory: [bold green]{total_obls}[/] obligations from {len(json_files)} documents")

    # Build / Append
    if index_exists and not force:
        console.print(f"\n  ➕  [bold]Appending new documents[/] to existing index...")
    else:
        console.print(f"\n  🔧  [bold]Building index[/] from scratch...")

    try:
        mgr = IndexManager()
        start = time.time()

        append_mode = index_exists and not force
        result_path = mgr.build_from_directory(
            str(scan_dir), index_path=index_dir, append=append_mode,
        )
        elapsed = time.time() - start

        info = IndexManager.get_index_info(result_path)
        console.print(f"\n  ✅ [green bold]Index {'updated' if append_mode else 'built'} successfully![/]")
        console.print(f"      Path: [dim]{result_path}[/]")
        console.print(f"      Documents indexed: [bold cyan]{info['num_docs']}[/]")
        console.print(f"      Size: [dim]{info['size_mb']} MB[/]")
        console.print(f"      Time: [dim]{elapsed:.1f}s[/]")

        console.print(f"\n  🚀  [green]Ready for comparison![/] "
                      f"Use [bold]--pipeline[/] or [bold]--summary-comparison[/] "
                      f"with [bold]--internal-dir {scan_dir}/[/]")
    except Exception as e:
        console.print(f"\n  ❌ [red]Index build failed: {e}[/]")
        sys.exit(1)


def run_index_info(internal_dir: str) -> None:
    """Show information about the BM25 index."""
    from tools.search.index_manager import IndexManager

    scan_dir = Path(internal_dir)
    index_dir = str(scan_dir / ".bm25_index")

    _banner("ℹ️", "BM25 INDEX INFO", f"📁  {index_dir}", color="blue")

    if not IndexManager.index_exists(index_dir):
        console.print(f"\n  ⚠️   [yellow]No index found at:[/] {index_dir}")
        console.print(f"  💡  Run [bold]--build-index --internal-dir {internal_dir}[/] to create one.")
        return

    info = IndexManager.get_index_info(index_dir)

    table = Table(title="📊  BM25 Index Status", border_style="blue", title_style="bold blue")
    table.add_column("", width=3, no_wrap=True)
    table.add_column("Metric", style="white")
    table.add_column("Value", style="bold")

    table.add_row("📁", "Path", f"[dim]{info['path']}[/]")
    table.add_row("📑", "Documents indexed", f"[bold cyan]{info['num_docs']}[/]")
    table.add_row("💾", "Size on disk", f"[dim]{info['size_mb']} MB[/]")
    table.add_row("✅", "Status", "[green]Ready[/]")

    # Show constituent documents by scanning split results
    json_files = sorted(scan_dir.glob("**/*_split_result.json"))
    if json_files:
        table.add_row("📦", "Source files", f"[dim]{len(json_files)} split results[/]")
        for fpath in json_files[:10]:
            try:
                data = json.loads(fpath.read_text(encoding="utf-8"))
                obls = data if isinstance(data, list) else data.get("obligations", [])
                rel = fpath.relative_to(scan_dir)
                table.add_row("", "", f"[dim]{rel}[/] → [cyan]{len(obls)}[/] obligations")
            except Exception:
                pass
        if len(json_files) > 10:
            table.add_row("", "", f"[dim]... and {len(json_files) - 10} more[/]")

    console.print()
    console.print(table)

    console.print(f"\n  💡  To rebuild: [bold]python run.py --build-index --internal-dir {internal_dir} --force[/]")


# ═══════════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════════

def run_summary_comparison(
    external_files: str = "",
    internal_files: str = "",
    external_raw_text: str = "",
    internal_raw_text: str = "",
    doc_name: str = "",
    output_dir: str = "",
) -> str:
    """Compare + report in one API call (POST /summary_comparison).

    No intermediate task IDs — one call, one result: compare data + Markdown report.
    """
    ext_paths = [f.strip() for f in external_files.split(",") if f.strip()]
    int_paths = [f.strip() for f in internal_files.split(",") if f.strip()]

    if not ext_paths or not int_paths:
        console.print(f"\n  {ICONS['cross']} [red]--external-files and --internal-files are required[/]")
        sys.exit(1)

    for f in ext_paths + int_paths:
        if not Path(f).exists():
            console.print(f"\n  {ICONS['cross']} [red]File not found:[/] {f}")
            sys.exit(1)

    ext_display = ", ".join(Path(f).name for f in ext_paths)
    int_display = ", ".join(Path(f).name for f in int_paths)

    _banner(ICONS["summary"], "Compare + Gap Analysis Report",
            f"{ICONS['link']}  external={ext_display}  |  internal={int_display}",
            color="magenta")

    console.print(f"\n  {ICONS['upload']}  [bold]Submitting summary_comparison...[/]")
    console.print(f"  {ICONS['bullet']}  External: [cyan]{ext_display}[/]")
    console.print(f"  {ICONS['bullet']}  Internal: [yellow]{int_display}[/]")

    # Build multipart form
    files_payload: list[tuple] = []
    for fpath in ext_paths:
        files_payload.append(("external_files", (Path(fpath).name, open(fpath, "rb"), "application/json")))
    for fpath in int_paths:
        files_payload.append(("internal_files", (Path(fpath).name, open(fpath, "rb"), "application/json")))

    data_payload = {
        "external_raw_text": external_raw_text,
        "internal_raw_text": internal_raw_text,
        "document_name": doc_name or _clean_doc_name(ext_paths[0]),
    }

    try:
        resp = _post("/summary_comparison", data=data_payload, files=files_payload)
    finally:
        for _, (_, f, _) in files_payload:
            f.close()

    task_id = resp["task_id"]
    console.print(f"  {ICONS['check']}  Task: [bold magenta]{task_id}[/]")

    out_label = _clean_doc_name(ext_paths[0]) if len(ext_paths) == 1 else "comparison"
    final = poll_task(task_id, label=f"comparison:{out_label}")

    # Download results
    base = Path(output_dir) if output_dir else OUTPUT_ROOT
    out_dir = base / out_label
    json_data = _get(f"/result/{task_id}/json")
    save_json(json_data, out_dir, f"{out_label}_比较报告.json")

    # Print compare summary from combined result
    compare_data = json_data.get("compare_result", {})
    if compare_data:
        _print_compare_summary(compare_data)

    # Save & preview report
    try:
        report_bytes = _get_raw(f"/result/{task_id}/report")
        save_binary(report_bytes, out_dir, "差距分析报告.md")
    except Exception as e:
        console.print(f"  {ICONS['warn']}  [dim]Report download failed: {e}[/]")

    report = json_data.get("report", "")
    if report:
        preview = report[:400]
        console.print()
        console.print(Panel(
            Text(preview + ("..." if len(report) > 400 else ""), style="white"),
            title=f"{ICONS['report']}  Report Preview ({len(report)} chars)",
            border_style="magenta",
        ))

    console.print(f"\n  {ICONS['check']}  [green bold]Done.[/] [dim]Compare + Report → {out_dir}/[/]")
    return task_id


# ═══════════════════════════════════════════════════════════════════════════
# Pipeline
# ═══════════════════════════════════════════════════════════════════════════

def run_pipeline(
    file_path: str,
    internal_files: str,
    internal_raw_text: str = "",
    doc_name: str = "",
    output_dir: str = "",
) -> None:
    """Run full pipeline: split → compare → summary (supports multi-file).

    Args:
        file_path: Comma-separated external regulation file paths.
        internal_files: Comma-separated internal obligation JSON paths.
        output_dir: Base directory for saving results (default: outputs/).
    """
    ext_files = [f.strip() for f in file_path.split(",") if f.strip()]
    int_files = [f.strip() for f in internal_files.split(",") if f.strip()]

    for f in ext_files:
        if not Path(f).exists():
            console.print(f"\n  {ICONS['cross']} [red]File not found:[/] {f}")
            sys.exit(1)
    for f in int_files:
        if not Path(f).exists():
            console.print(f"\n  {ICONS['cross']} [red]File not found:[/] {f}")
            sys.exit(1)

    fext_icon = _file_icon(ext_files[0])
    fint_icon = _file_icon(int_files[0])

    ext_display = ", ".join(Path(f).name for f in ext_files)
    int_display = ", ".join(Path(f).name for f in int_files)

    _banner(ICONS["pipeline"], "FULL PIPELINE  —  Split → Compare → Report",
            f"{fext_icon}  external={ext_display}  |  {fint_icon}  internal={int_display}",
            color="green")

    console.print(f"\n  {ICONS['rocket']}  [bold]Launching pipeline...[/]")

    # Build multipart form
    files_payload: list[tuple] = []
    for fpath in ext_files:
        files_payload.append(("files", (Path(fpath).name, open(fpath, "rb"), "application/octet-stream")))
    for fpath in int_files:
        files_payload.append(("internal_files", (Path(fpath).name, open(fpath, "rb"), "application/json")))

    data_payload = {
        "document_name": doc_name or Path(ext_files[0]).stem,
        "internal_raw_text": internal_raw_text,
    }

    try:
        resp = _post("/pipeline", data=data_payload, files=files_payload)
    finally:
        for _, (_, f, _) in files_payload:
            f.close()

    task_id = resp["task_id"]
    console.print(f"  {ICONS['check']}  Pipeline task: [bold green]{task_id}[/]")
    console.print(f"  {ICONS['bullet']}  [dim]{resp['message']}[/]")

    final = poll_task(task_id, label=f"pipeline:{Path(ext_files[0]).stem}")
    doc_name_final = final.get("document_name", Path(ext_files[0]).stem)

    base = Path(output_dir) if output_dir else OUTPUT_ROOT
    out_dir = base / Path(doc_name_final).stem
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
    table.add_column("", width=3, no_wrap=True)
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
# Chain (client-side orchestration: split → compare → summary)
# ═══════════════════════════════════════════════════════════════════════════

def run_chain(
    file_path: str,
    internal_files: str,
    internal_raw_text: str = "",
    doc_name: str = "",
    output_dir: str = "",
) -> None:
    """Run split → compare → summary on the client side (file-based, no task IDs).

    1. Split raw doc → download split result JSON
    2. Compare using the downloaded JSON as --external-files
    3. Summary using the compare result

    Contrast with --pipeline:
      - --chain: 3 API calls, intermediate results visible, can resume
      - --pipeline: 1 API call, results only at the end
    """
    if not Path(file_path).exists():
        console.print(f"\n  {ICONS['cross']} [red]File not found:[/] {file_path}")
        sys.exit(1)

    int_paths = [f.strip() for f in internal_files.split(",") if f.strip()]
    for f in int_paths:
        if not Path(f).exists():
            console.print(f"\n  {ICONS['cross']} [red]File not found:[/] {f}")
            sys.exit(1)

    _banner(ICONS["rocket"], "CHAIN MODE — Client-Side Orchestration",
            f"Split → Compare → Summary  |  {_file_icon(file_path)}  {Path(file_path).name}  |  "
            f"{_file_icon(int_paths[0])}  {len(int_paths)} internal file(s)",
            color="green")

    chain_failed = False
    ext_split_file = ""  # path to downloaded split result JSON

    # ── Phase 1: Split ──────────────────────────────────────────────────
    split_id = ""
    try:
        console.print(f"\n  {ICONS['star']}  [bold cyan]Phase 1/3: Splitting...[/]")
        split_id = run_split(file_path, output_dir=output_dir)

        # Download split result for next phase
        json_data = _get(f"/result/{split_id}/json")
        base = Path(output_dir) if output_dir else OUTPUT_ROOT
        out_dir = base / Path(file_path).stem
        out_dir.mkdir(parents=True, exist_ok=True)
        ext_split_file = str(out_dir / f"{Path(file_path).stem}_split_result.json")
        Path(ext_split_file).write_text(json.dumps(json_data, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"  {ICONS['check']}  Split complete, result saved → [dim]{ext_split_file}[/]")
    except SystemExit:
        split_id = ""
    except Exception as e:
        console.print(f"  {ICONS['cross']}  [red]Split failed: {e}[/]")
        chain_failed = True

    if not ext_split_file or chain_failed:
        console.print(f"\n  {ICONS['cross']}  [red bold]Chain stopped at Phase 1 (Split).[/]")
        sys.exit(1)

    # ── Phase 2+3: Summary Comparison (compare + report in one call) ──
    compare_id = ""
    try:
        console.print(f"\n  {ICONS['star']}  [bold yellow]Phase 2/3: Compare + Report...[/]")
        compare_id = run_summary_comparison(
            external_files=ext_split_file,
            internal_files=internal_files,
            external_raw_text="",
            internal_raw_text=internal_raw_text or "",
            doc_name=doc_name or Path(file_path).stem,
            output_dir=output_dir,
        )
    except SystemExit:
        compare_id = ""
    except Exception as e:
        console.print(f"  {ICONS['cross']}  [red]Summary comparison failed: {e}[/]")
        chain_failed = True

    if not compare_id or chain_failed:
        console.print(f"\n  {ICONS['cross']}  [red bold]Chain stopped at Phase 2 (Summary Comparison).[/]")
        console.print(f"  💡  Split result saved at: {ext_split_file}")
        console.print(f"      Resume: python test_app.py --summary-comparison --external-files {ext_split_file} --internal-files {internal_files}")
        sys.exit(1)

    # ── Done ─────────────────────────────────────────────────────────────
    console.print()
    console.print(Panel(
        Text(f"All phases completed successfully!\n\n"
             f"  Results:  outputs/{Path(file_path).stem}/",
             style="green"),
        title="✅  CHAIN COMPLETE", border_style="green",
    ))


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
  # ── Offline: Split + Index internal regulations ────────
  python run.py --pre-split --file 内规库.xlsx --output-dir outputs/internal
  python run.py --build-index --internal-dir outputs/internal/

  # ── Online: Compare external vs internal ───────────────
  python run.py --pipeline --file 外规.docx --internal-dir outputs/internal/ --output-dir outputs/external

  # ── Index management ───────────────────────────────────
  python run.py --index-info --internal-dir outputs/internal/
  python run.py --build-index --internal-dir outputs/internal/ --force   # rebuild

  python run.py --list          # show all tasks
        """,
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--split", action="store_true", help="📝  Clause splitting only")
    mode.add_argument("--pre-split", action="store_true", help="📑  Pre-split large multi-doc Excel by document name, then split each")
    mode.add_argument("--summary-comparison", action="store_true", help="📋  Compare + Gap analysis report (one call)")
    mode.add_argument("--pipeline", action="store_true", help="🚀  Full pipeline — server-side (split → compare → report)")
    mode.add_argument("--chain", action="store_true", help="🔗  Chain mode — client-side orchestration, shows intermediate results")
    mode.add_argument("--list", nargs="?", const="", metavar="TYPE",
                      help="📋  List all tasks (optional: filter by type)")
    mode.add_argument("--clear", action="store_true", help="🗑️   Clear all task records")
    mode.add_argument("--build-index", action="store_true", help="🔧  Build persistent BM25 index from internal split results (offline, run once)")
    mode.add_argument("--index-info", action="store_true", help="ℹ️   Show BM25 index status and statistics")

    parser.add_argument("--base-url", default=BASE_URL, help=f"API base URL (default: {BASE_URL})")
    parser.add_argument("--file", help="📄  Raw regulation file for --split/--chain/--pipeline (comma-sep for multi)")
    parser.add_argument("--document-name", default="", help="Override document name")

    parser.add_argument("--external-files", help="📦  External obligation JSON file(s) for --compare/--summary (comma-sep)")
    parser.add_argument("--internal-files", default="", help="📦  Internal obligation JSON file(s) for --compare/--summary/--chain/--pipeline (comma-sep)")
    parser.add_argument("--internal-dir", default="", help="📁  Directory of internal *_split_result.json files (alternative to --internal-files)")

    parser.add_argument("--output-dir", default="", help="📁  Output directory for split results (default: outputs/)")
    parser.add_argument("--force", action="store_true", help="💪  Force re-process all documents (ignore existing results)")
    parser.add_argument("--external-text", default="", help="External regulation raw text (for --summary)")
    parser.add_argument("--internal-text", default="", help="Internal regulation raw text (for --summary)")

    args = parser.parse_args()
    BASE_URL = args.base_url

    if args.clear:
        resp = requests.delete(f"{BASE_URL}/tasks")
        resp.raise_for_status()
        data = resp.json()
        console.print(f"\n  🗑️  {data['message']}")
        return

    # ── Resolve internal files from --internal-files or --internal-dir ──
    def _resolve_internal_files(files: str, directory: str) -> str:
        """Return comma-separated paths from --internal-files or --internal-dir."""
        if directory:
            d = Path(directory)
            if not d.is_dir():
                console.print(f"\n  ❌ [red]Not a directory:[/] {directory}")
                sys.exit(1)
            json_files = sorted(d.glob("**/*_split_result.json"))
            if not json_files:
                console.print(f"\n  ❌ [red]No *_split_result.json files found in:[/] {directory}")
                sys.exit(1)
            return ",".join(str(f) for f in json_files)
        return files

    internal_files = _resolve_internal_files(args.internal_files, args.internal_dir)

    # Show resolved paths
    _resolved_output = Path(args.output_dir) if args.output_dir else OUTPUT_ROOT
    console.print(f"  📁  Output dir: [cyan]{_resolved_output}[/]")

    if not any([args.split, getattr(args, 'pre_split', False), getattr(args, 'summary_comparison', False), args.pipeline, args.chain, args.list is not None, args.build_index, args.index_info]):
        if args.file:
            args.split = True
        else:
            parser.print_help()
            console.print(f"\n  💡 [dim]Tip: use --split --file <path> for a quick test, or --list to see tasks[/]")
            sys.exit(1)

    console.print(f"\n  🌐 Server: [bold cyan]{BASE_URL}[/]")

    if args.list is not None:
        run_list(task_type=args.list)
    elif getattr(args, 'pre_split', False):
        if not args.file:
            console.print(f"\n  ❌ [red]--file is required for --pre-split[/]")
            sys.exit(1)
        run_pre_split(args.file, output_dir=args.output_dir, force=args.force)
    elif args.build_index:
        if not args.internal_dir:
            console.print(f"\n  ❌ [red]--internal-dir is required for --build-index[/]")
            sys.exit(1)
        run_build_index(args.internal_dir, force=args.force)
    elif args.index_info:
        if not args.internal_dir:
            console.print(f"\n  ❌ [red]--internal-dir is required for --index-info[/]")
            sys.exit(1)
        run_index_info(args.internal_dir)
    elif args.split:
        if not args.file:
            console.print(f"\n  ❌ [red]--file is required for --split[/]")
            sys.exit(1)
        run_split(args.file, output_dir=args.output_dir)
    elif getattr(args, 'summary_comparison', False):
        if not args.external_files or not internal_files:
            console.print(f"\n  ❌ [red]--external-files and (--internal-files or --internal-dir) are required[/]")
            sys.exit(1)
        run_summary_comparison(args.external_files, internal_files,
                               args.external_text, args.internal_text, args.document_name,
                               output_dir=args.output_dir)
    elif args.pipeline:
        if not args.file or not internal_files:
            console.print(f"\n  ❌ [red]--file and (--internal-files or --internal-dir) are required[/]")
            sys.exit(1)
        run_pipeline(args.file, internal_files, args.internal_text,
                    args.document_name, output_dir=args.output_dir)
    elif args.chain:
        if not args.file or not internal_files:
            console.print(f"\n  ❌ [red]--file and (--internal-files or --internal-dir) are required[/]")
            sys.exit(1)
        run_chain(args.file, internal_files, args.internal_text,
                  args.document_name, output_dir=args.output_dir)


if __name__ == "__main__":
    main()
