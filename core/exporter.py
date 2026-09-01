"""Excel 导出。

需求里写死了三列，所以这里也写死三列——不多、不少、不加序号、不加时间戳列。
任何"顺手加个字段"的冲动都请忍住：字段一多，下游的人就得先删列才能用。
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Sequence, Tuple

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .models import Summary

HEADERS: Tuple[str, str, str] = ("客户名称", "时间范围", "总结内容")
WIDTHS = (26, 26, 92)

_HEAD_FILL = PatternFill("solid", fgColor="1F5FA5")
_HEAD_FONT = Font(name="微软雅黑", size=11, bold=True, color="FFFFFF")
_BODY_FONT = Font(name="微软雅黑", size=10)
_THIN = Side(style="thin", color="D9D9D9")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


def _style_sheet(ws) -> None:
    for idx, (title, width) in enumerate(zip(HEADERS, WIDTHS), start=1):
        cell = ws.cell(row=1, column=idx, value=title)
        cell.fill = _HEAD_FILL
        cell.font = _HEAD_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = _BORDER
        ws.column_dimensions[get_column_letter(idx)].width = width
    ws.row_dimensions[1].height = 26
    ws.freeze_panes = "A2"


def _header_matches(ws) -> bool:
    got = tuple((ws.cell(row=1, column=i).value or "") for i in range(1, 4))
    return got == HEADERS


def export(summaries: Sequence[Summary], path: str | Path,
           mode: str = "append") -> Tuple[Path, int, int]:
    """写入 Excel。

    mode='append'    追加到已有文件末尾（表头一致时），文件不存在则新建
    mode='overwrite' 覆盖重建

    返回 (实际写入路径, 本次写入行数, 写完后总数据行数)
    """
    path = Path(path)
    if path.suffix.lower() not in (".xlsx", ".xlsm"):
        path = path.with_suffix(".xlsx")
    path.parent.mkdir(parents=True, exist_ok=True)

    wb = None
    if mode == "append" and path.exists():
        try:
            wb = load_workbook(path)
            ws = wb.active
            if not _header_matches(ws):
                # 已有文件表头不符（可能是别的表），另存新文件，不破坏原数据
                wb = None
                path = _unique(path)
        except Exception:
            wb = None
            path = _unique(path)

    if wb is None:
        wb = Workbook()
        ws = wb.active
        ws.title = "客户沟通总结"
        _style_sheet(ws)
    else:
        ws = wb.active

    start_row = ws.max_row + 1 if ws.max_row >= 1 else 2
    if ws.max_row == 1 and ws.cell(row=1, column=1).value is None:
        _style_sheet(ws)
        start_row = 2

    written = 0
    for i, s in enumerate(summaries):
        r = start_row + i
        for c, value in enumerate((s.customer_name, s.time_range, s.content), start=1):
            cell = ws.cell(row=r, column=c, value=value)
            cell.font = _BODY_FONT
            cell.border = _BORDER
            cell.alignment = Alignment(
                horizontal="left" if c == 3 else "center",
                vertical="center" if c != 3 else "top",
                wrap_text=(c == 3),
            )
        ws.row_dimensions[r].height = max(30, min(120, (len(s.content) // 40 + 1) * 20))
        written += 1

    try:
        wb.save(path)
    except PermissionError as e:
        raise PermissionError(
            f"无法写入「{path.name}」。该文件可能正被 Excel 打开，请关闭后重试。"
        ) from e
    return path, written, max(ws.max_row - 1, 0)


def _unique(path: Path) -> Path:
    stem, suffix, parent = path.stem, path.suffix, path.parent
    for i in range(2, 200):
        cand = parent / f"{stem}_{i}{suffix}"
        if not cand.exists():
            return cand
    return parent / f"{stem}_new{suffix}"


def preview_rows(summaries: Sequence[Summary]) -> List[Tuple[str, str, str]]:
    return [(s.customer_name, s.time_range, s.content) for s in summaries]
