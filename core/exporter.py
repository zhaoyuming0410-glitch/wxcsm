"""Excel 导出。

需求里写死了三列，所以这里也写死三列——不多、不少、不加序号、不加时间戳列。
任何"顺手加个字段"的冲动都请忍住：字段一多，下游的人就得先删列才能用。

后来又加了第二种输出形态「客户问题答复清单」（一个对象产出多行），
它独立成一张表，不复用上面三列的结构——把问答塞进"总结内容"一列等于没做。
两张表可以共存在同一个工作簿里，互不干扰。
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Sequence, Tuple

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .models import QA_STATUS_DONE, QaItem, Summary

# XML 1.0 合法字符(这里只排除控制字符);Excel/xlsx 单元格不能含这些非法字节
_ILLEGAL_CONTROL = dict.fromkeys(range(32))  # 0x00-0x1f
for _c in range(0x7F, 0xA0):
    _ILLEGAL_CONTROL.setdefault(_c, None)  # 0x7f-0x9f 也大多是控制字符

def _clean(value) -> str:
    """剔除 openpyxl 写入 xlsx 时非法/不可用的控制字符, 避免保存时抛出错误。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value.translate(_ILLEGAL_CONTROL).replace("\uFFFD", "")


SUMMARY_SHEET = "客户沟通总结"
QA_SHEET = "客户问题答复清单"

HEADERS: Tuple[str, str, str] = ("客户名称", "时间范围", "总结内容")
WIDTHS = (26, 26, 92)

# 问答清单：一行一个问题。列较多但每列都有明确用途——
# 「原始记录」放双方原话，是为了让抽取结果可回溯核对（抽取是规则+模型做的，必须能验）。
QA_HEADERS: Tuple[str, ...] = ("客户名称", "类型", "提问时间", "客户问题",
                               "我方答复/处理", "答复时间", "状态", "原始记录")
QA_WIDTHS = (20, 11, 13, 44, 54, 13, 17, 60)

_HEAD_FILL = PatternFill("solid", fgColor="1F5FA5")
_HEAD_FONT = Font(name="微软雅黑", size=11, bold=True, color="FFFFFF")
_BODY_FONT = Font(name="微软雅黑", size=10)
_WARN_FONT = Font(name="微软雅黑", size=10, color="C00000")    # 未答复：标记红字
_PROMISE_FONT = Font(name="微软雅黑", size=10, color="BF6000")  # 我方待办：橙字
_THIN = Side(style="thin", color="D9D9D9")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


def _style_sheet(ws, headers: Sequence[str], widths: Sequence[int]) -> None:
    for idx, (title, width) in enumerate(zip(headers, widths), start=1):
        cell = ws.cell(row=1, column=idx, value=title)
        cell.fill = _HEAD_FILL
        cell.font = _HEAD_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = _BORDER
        ws.column_dimensions[get_column_letter(idx)].width = width
    ws.row_dimensions[1].height = 26
    ws.freeze_panes = "A2"


def _header_matches(ws, headers: Sequence[str]) -> bool:
    got = tuple((ws.cell(row=1, column=i).value or "") for i in range(1, len(headers) + 1))
    return got == tuple(headers)


def _next_row(ws) -> int:
    start_row = ws.max_row + 1 if ws.max_row >= 1 else 2
    if ws.max_row == 1 and ws.cell(row=1, column=1).value is None:
        start_row = 2
    return start_row


def _reset_sheet(wb, title: str, headers: Sequence[str], widths: Sequence[int]):
    """把工作簿里已有的某张表清空重建（丢掉旧数据与旧格式，保留原表位置）。

    关键：**只动这一张表**。早先 overwrite 是从零 new 一个 Workbook 再存回原路径，
    结果是同一文件里的另一张表（问答清单）被整张抹掉——一次"覆盖重建总结"
    就把问答数据全丢了。改成删除单张表再原地重建，兄弟表不受影响。
    """
    idx = wb.sheetnames.index(title)
    del wb[title]
    ws = wb.create_sheet(title, idx)
    _style_sheet(ws, headers, widths)
    return ws


def _open_target(path: Path, sheet_title: str, headers: Sequence[str],
                 widths: Sequence[int], mode: str):
    """决定这次写入落在哪里，返回 (workbook, worksheet, 实际路径)。

    mode='append'    已有同表头的表就追加；缺这张表就在同一工作簿里补建
    mode='overwrite' 已有这张表就清空重建；缺这张表就补建

    两种情况共同遵守的一条底线：**只操作我们自己的两张表，绝不改动别人的数据**。
    所以「目标文件是个陌生工作簿」和「同名表但表头不是我们的」都走另存新文件，
    宁可多出一个文件，也不能破坏用户已有的表。
    """
    if path.exists():
        wb = None
        try:
            wb = load_workbook(path)
        except Exception:
            wb = None                        # 文件损坏/不是 xlsx → 另存
        if wb is not None:
            ours = set(wb.sheetnames) & {SUMMARY_SHEET, QA_SHEET}
            if sheet_title in wb.sheetnames:
                if _header_matches(wb[sheet_title], headers):
                    if mode == "overwrite":
                        return wb, _reset_sheet(wb, sheet_title, headers, widths), path
                    return wb, wb[sheet_title], path
                wb = None                    # 同名表但不是我们的 → 另存
            elif ours:
                # 是我们生成的工作簿，但还没有这张表 → 补建，不要另存出一个 _2 文件。
                # 总结表固定放最前，保持"先总结、后问答"的阅读顺序。
                ws = wb.create_sheet(
                    sheet_title, 0 if sheet_title == SUMMARY_SHEET else None)
                _style_sheet(ws, headers, widths)
                return wb, ws, path
            else:
                wb = None                    # 陌生工作簿 → 另存
        if wb is None:
            path = _unique(path)

    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title
    _style_sheet(ws, headers, widths)
    return wb, ws, path


def _save(wb, path: Path) -> None:
    try:
        wb.save(path)
    except PermissionError as e:
        raise PermissionError(
            f"无法写入「{path.name}」。该文件可能正被 Excel 打开，请关闭后重试。"
        ) from e


def export(summaries: Sequence[Summary], path: str | Path,
           mode: str = "append") -> Tuple[Path, int, int]:
    """写入「客户沟通总结」表。

    mode='append'    追加到已有文件末尾（表头一致时），文件不存在则新建
    mode='overwrite' 覆盖重建

    返回 (实际写入路径, 本次写入行数, 写完后总数据行数)
    """
    path = Path(path)
    if path.suffix.lower() not in (".xlsx", ".xlsm"):
        path = path.with_suffix(".xlsx")
    path.parent.mkdir(parents=True, exist_ok=True)

    wb, ws, path = _open_target(path, SUMMARY_SHEET, HEADERS, WIDTHS, mode)
    start_row = _next_row(ws)

    written = 0
    for i, s in enumerate(summaries):
        r = start_row + i
        row_values = (_clean(s.customer_name), _clean(s.time_range), _clean(s.content))
        for c, value in enumerate(row_values, start=1):
            cell = ws.cell(row=r, column=c, value=value)
            cell.font = _BODY_FONT
            cell.border = _BORDER
            cell.alignment = Alignment(
                horizontal="left" if c == 3 else "center",
                vertical="center" if c != 3 else "top",
                wrap_text=(c == 3),
            )
        ws.row_dimensions[r].height = max(30, min(120, (len(_clean(s.content)) // 40 + 1) * 20))
        written += 1

    _save(wb, path)
    return path, written, max(ws.max_row - 1, 0)


def export_qa(items: Sequence[QaItem], path: str | Path,
              mode: str = "append") -> Tuple[Path, int, int]:
    """写入「客户问题答复清单」表。

    参数与 export() 一致，追加/覆盖语义也一致；两张表各自独立判表头，
    所以可以用不同的 mode 分别写，互不影响。
    """
    path = Path(path)
    if path.suffix.lower() not in (".xlsx", ".xlsm"):
        path = path.with_suffix(".xlsx")
    path.parent.mkdir(parents=True, exist_ok=True)

    wb, ws, path = _open_target(path, QA_SHEET, QA_HEADERS, QA_WIDTHS, mode)
    start_row = _next_row(ws)

    written = 0
    for i, it in enumerate(items):
        r = start_row + i
        row_values = [_clean(v) for v in (
            it.customer_name, it.kind, it.ask_time, it.question_cell,
            it.answer, it.answer_time, it.status, it.raw,
        )]
        for c, value in enumerate(row_values, start=1):
            cell = ws.cell(row=r, column=c, value=value)
            cell.font = _BODY_FONT
            cell.border = _BORDER
            # 客户名称/类型/时间/状态居中，问题/答复/原始记录左对齐并自动换行
            wide = c in (4, 5, 8)
            cell.alignment = Alignment(
                horizontal="left" if wide else "center",
                vertical="top" if wide else "center",
                wrap_text=wide,
            )
        # 状态列上色：未答复标红、我方待办标橙，扫一眼就知道哪些还没闭环
        if it.status != QA_STATUS_DONE:
            ws.cell(row=r, column=7).font = (
                _PROMISE_FONT if it.kind == "我方待办" else _WARN_FONT)
        longest = max(len(row_values[3]), len(row_values[4]))
        ws.row_dimensions[r].height = max(30, min(150, (longest // 30 + 1) * 18))
        written += 1

    _save(wb, path)
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
