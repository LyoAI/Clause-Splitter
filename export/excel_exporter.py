"""Excel 导出器 —— 严格按模板样式生成 Excel 文件.

核心策略：template-copy-then-fill
1. 打开模板文件 → 保留1-4行表头
2. 从第5行开始逐行填充数据
3. 每行复制模板中已有的样式（字体、颜色、边框、对齐）
"""

from __future__ import annotations

from copy import copy
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.styles import (
    Font, PatternFill, Alignment, Border, Side,
)
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from utils import get_logger

logger = get_logger()


# ── Column mapping: English field → Excel column letter ───────────────────

COLUMN_MAP: dict[str, str] = {
    "index": "A",
    "trigger_condition": "B",
    "trigger_sub_condition": "C",
    "subject": "D",
    "object": "E",
    "obligation_rule": "F",
    "action_verb": "G",
    "action_target": "H",
    "method": "I",
    "document_name": "J",
    "clause_number": "K",
    "source_text": "L",
    "ambiguity_note": "M",
}

FIELD_ORDER: list[str] = [
    "index", "trigger_condition", "trigger_sub_condition", "subject", "object",
    "obligation_rule", "action_verb", "action_target", "method",
    "document_name", "clause_number", "source_text", "ambiguity_note",
]


class ExcelExporter:
    """Excel 导出器 —— 从模板复制样式，填充数据."""

    HEADER_ROWS = 4  # 表头行数（1-4行）

    def __init__(self, template_path: str | Path) -> None:
        """初始化导出器.

        Args:
            template_path: Excel 模板文件路径。
        """
        self.template_path = Path(template_path)
        if not self.template_path.exists():
            raise FileNotFoundError(f"Excel 模板不存在: {self.template_path}")

        # 从模板提取样式信息
        self._template_styles: dict[str, Any] = {}
        self._column_widths: dict[str, float] = {}
        self._extract_template_styles()

    def _extract_template_styles(self) -> None:
        """从模板提取样式参考."""
        wb = openpyxl.load_workbook(self.template_path)
        ws = wb.active

        # 提取列宽
        for col_letter in ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M"]:
            dim = ws.column_dimensions.get(col_letter)
            if dim and dim.width:
                self._column_widths[col_letter] = dim.width

        # 从第5行提取数据行参考样式（如果有示例行）
        ref_row = 5
        for col_letter in ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M"]:
            cell = ws.cell(row=ref_row, column=openpyxl.utils.column_index_from_string(col_letter))
            self._template_styles[col_letter] = {
                "font": copy(cell.font) if cell.font else Font(name="Baskerville", size=12),
                "fill": copy(cell.fill) if cell.fill else PatternFill(),
                "alignment": copy(cell.alignment) if cell.alignment else Alignment(horizontal="center", vertical="center"),
                "border": copy(cell.border) if cell.border else Border(),
            }

        wb.close()
        logger.info("模板样式提取完成: %d 列宽, %d 列样式", len(self._column_widths), len(self._template_styles))

    def export(
        self,
        obligations: list[dict[str, Any]],
        output_path: str | Path,
        document_name: str = "",
    ) -> str:
        """导出 Excel 文件.

        Args:
            obligations: ClauseObligation dict 列表。
            output_path: 输出文件路径。
            document_name: 文档名称（用于日志）。

        Returns:
            输出文件的绝对路径。
        """
        output_path = Path(output_path)

        # 方法1: 直接在模板上写（推荐，完美保留样式）
        try:
            self._export_on_template(obligations, output_path)
        except Exception as e:
            logger.warning("在模板上写入失败，回退到新建模式: %s", e)
            # 方法2: 新建 workbook，手动复制表头样式
            self._export_from_scratch(obligations, output_path)

        logger.info("Excel 导出完成: %s (%d 条记录)", output_path, len(obligations))
        return str(output_path.absolute())

    def _export_on_template(
        self,
        obligations: list[dict[str, Any]],
        output_path: Path,
    ) -> None:
        """在模板文件上直接写入数据（最佳方式，保留所有样式）."""
        import shutil

        # 复制模板
        shutil.copy2(self.template_path, output_path)

        wb = openpyxl.load_workbook(output_path)
        ws = wb.active

        # 清除旧的数据行（从第5行开始）
        if ws.max_row > self.HEADER_ROWS:
            ws.delete_rows(self.HEADER_ROWS + 1, ws.max_row - self.HEADER_ROWS)

        # 获取参考样式（从模板第5行或第1行）
        ref_styles: dict[str, dict] = {}
        for col_idx, field in enumerate(FIELD_ORDER, 1):
            col_letter = get_column_letter(col_idx)
            # 优先用第5行样式，不行就用第4行
            ref_row = 5 if ws.cell(row=5, column=col_idx).value is None else 4
            ref_cell = ws.cell(row=ref_row, column=col_idx)
            ref_styles[col_letter] = {
                "font": copy(ref_cell.font),
                "fill": copy(ref_cell.fill),
                "alignment": copy(ref_cell.alignment),
                "border": copy(ref_cell.border) if ref_cell.border else Border(
                    left=Side(style="thin"),
                    right=Side(style="thin"),
                    top=Side(style="thin"),
                    bottom=Side(style="thin"),
                ),
            }

        # Unmerge any existing merged ranges that overlap with data rows
        # (template may have example data rows with merged cells)
        for merged_range in list(ws.merged_cells.ranges):
            if merged_range.min_row > self.HEADER_ROWS:
                ws.unmerge_cells(str(merged_range))

        # Write data — each column gets its own value, no merging on data rows
        for i, obl in enumerate(obligations):
            row = self.HEADER_ROWS + 1 + i

            for col_idx, field in enumerate(FIELD_ORDER, 1):
                col_letter = get_column_letter(col_idx)
                cell = ws.cell(row=row, column=col_idx)

                value = self._get_field_value(obl, field)
                cell.value = value

                # Apply styles
                style = ref_styles.get(col_letter, {})
                cell.font = copy(style.get("font", Font(name="Baskerville", size=12)))
                cell.alignment = copy(style.get("alignment", Alignment(
                    horizontal="center", vertical="center", wrap_text=True,
                )))
                cell.border = copy(style.get("border", Border()))

        # 设置列宽
        for col_letter, width in self._column_widths.items():
            ws.column_dimensions[col_letter].width = width

        # 设置行高
        for i in range(len(obligations)):
            row = self.HEADER_ROWS + 1 + i
            ws.row_dimensions[row].height = 30

        wb.save(output_path)
        wb.close()

    def _export_from_scratch(
        self,
        obligations: list[dict[str, Any]],
        output_path: Path,
    ) -> None:
        """新建 workbook（回退方案）."""
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.title = "合规义务拆分"

        # 写入表头
        self._write_header(ws)

        # Write values — no merging on data rows (header already has visual groups)
        for i, obl in enumerate(obligations):
            row = self.HEADER_ROWS + 1 + i
            ws.row_dimensions[row].height = 30

            for col_idx, field in enumerate(FIELD_ORDER, 1):
                cell = ws.cell(row=row, column=col_idx)
                cell.value = self._get_field_value(obl, field)
                cell.font = Font(name="Baskerville", size=12)
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                cell.border = Border(
                    left=Side(style="thin"),
                    right=Side(style="thin"),
                    top=Side(style="thin"),
                    bottom=Side(style="thin"),
                )

        # Set column widths from template
        for col_letter, width in self._column_widths.items():
            ws.column_dimensions[col_letter].width = width

        wb.save(output_path)
        wb.close()

    def _write_header(self, ws: Worksheet) -> None:
        """写入 Excel 表头（4行，13列 A-M）."""
        # Row 1 — 主标题
        ws.merge_cells("A1:A4")
        ws["A1"] = "序号"
        ws.merge_cells("B1:I1")
        ws["B1"] = "监管要求"
        ws.merge_cells("J1:L1")
        ws["J1"] = "要求出处"
        ws["M1"] = "歧义说明"

        # Row 2 — 分组标题
        ws.merge_cells("B2:C2")
        ws["B2"] = "触发义务的条件"
        ws["D2"] = "义务主体"
        ws["E2"] = "义务客体"
        ws.merge_cells("F2:H2")
        ws["F2"] = "义务内容"
        ws["I2"] = "实现义务的方式"

        # Row 3 — 字段标签
        ws["B3"] = "在什么时间、地点、场景"
        ws["C3"] = "在什么时间、地点、场景（子条件）"
        ws["D3"] = "谁"
        ws["E3"] = "向什么样的对象"
        ws["F3"] = "应当/禁止（枚举指）"
        ws["G3"] = "做"
        ws["H3"] = "什么"
        ws["I3"] = "通过什么方式"
        ws["J3"] = "文件名称"
        ws["K3"] = "条款编号"
        ws["L3"] = "引用原文"
        ws["M3"] = "有歧义/无歧义（枚举值）"

        # Row 4 — 语法提示
        ws["B4"] = "（时间/地点/场景状语）"
        ws["C4"] = "（时间/地点/场景状语）"
        ws["D4"] = "主体"
        ws["E4"] = "定语/宾语"
        ws["F4"] = "/"
        ws["G4"] = "谓语"
        ws["H4"] = "状语"
        ws["I4"] = "条件状语"
        ws["J4"] = ws["K4"] = ws["L4"] = ws["M4"] = "/"

        # 样式
        header_font = Font(name="Baskerville", size=12)
        for row in range(1, 5):
            for col in range(1, 14):
                cell = ws.cell(row=row, column=col)
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    @staticmethod
    def _get_field_value(obl: dict[str, Any], field: str) -> Any:
        """Safely extract a field value from an obligation dict."""
        value = obl.get(field, "/")
        if value is None:
            return "/"
        if hasattr(value, "value"):  # Enum → string
            return value.value
        return value if value != "" else "/"
