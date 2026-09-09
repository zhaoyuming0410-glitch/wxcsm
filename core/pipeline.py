"""业务流水线：GUI 与 CLI 共用同一套逻辑，避免两边行为不一致。

流程固定为四步：
    选数据源 → 取聊天对象 → 按时间范围抓消息 → 逐组生成总结 → 写 Excel
每一组「聊天对象 + 时间范围」产出恰好一篇总结，对应 Excel 的一行。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import exporter
from .models import ChatBundle, Contact, Summary
from .sources import ChatSource, SourceError, create
from .summarizer import summarize

ProgressFn = Callable[[int, int, str], None]


# ---------- 时间范围快捷方式 ----------
def preset_range(key: str, today: Optional[date] = None) -> Tuple[date, date]:
    t = today or date.today()
    if key == "today":
        return t, t
    if key == "yesterday":
        y = t - timedelta(days=1)
        return y, y
    if key == "7d":
        return t - timedelta(days=6), t
    if key == "30d":
        return t - timedelta(days=29), t
    if key == "this_month":
        return t.replace(day=1), t
    if key == "last_month":
        first_this = t.replace(day=1)
        last_end = first_this - timedelta(days=1)
        return last_end.replace(day=1), last_end
    if key == "this_quarter":
        q_start_month = 3 * ((t.month - 1) // 3) + 1
        return t.replace(month=q_start_month, day=1), t
    raise ValueError(f"未知时间范围快捷方式：{key}")


PRESETS: List[Tuple[str, str]] = [
    ("7d", "近 7 天"), ("30d", "近 30 天"), ("this_month", "本月"),
    ("last_month", "上月"), ("this_quarter", "本季度"), ("today", "今天"),
]


def parse_day(value: str, field: str = "日期") -> date:
    s = (value or "").strip().replace("/", "-").replace(".", "-")
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"{field}格式不正确：「{value}」，请使用 2026-08-01 这样的格式。")


# ---------- 结果容器 ----------
@dataclass
class RunResult:
    summaries: List[Summary]
    skipped: List[str]
    export_path: Optional[str] = None
    written: int = 0
    total_rows: int = 0

    @property
    def ok_count(self) -> int:
        return len(self.summaries)


# ---------- 主流程 ----------
def build_source(cfg: Dict[str, Any],
                 progress_cb: Optional[Callable[[str], None]] = None) -> ChatSource:
    opts = dict(self_names=cfg.get("self_names") or [])
    if progress_cb:
        opts["progress_cb"] = progress_cb
    return create(cfg.get("source", "demo"), path=cfg.get("source_path", ""), **opts)


def load_contacts(cfg: Dict[str, Any],
                  progress_cb: Optional[Callable[[str], None]] = None) -> List[Contact]:
    src = build_source(cfg, progress_cb=progress_cb)
    ok, msg = src.health()
    if not ok:
        raise SourceError(msg)
    return src.list_contacts()


def filter_contacts(contacts: Iterable[Contact], keyword: str) -> List[Contact]:
    return [c for c in contacts if c.matches(keyword)]


def run(cfg: Dict[str, Any], contacts: Sequence[Contact], start: date, end: date,
        progress: Optional[ProgressFn] = None,
        do_export: bool = True) -> RunResult:
    """对每个聊天对象抓取区间消息并生成一篇总结。"""
    if start > end:
        raise ValueError("开始日期不能晚于结束日期。")
    if not contacts:
        raise ValueError("请至少选择一个聊天对象。")

    total = len(contacts)
    # 前置步骤(取钥/解密)的进度: 进度条保持 0, 只更新状态栏文字, 避免误示处理已完成
    src = build_source(cfg, progress_cb=(lambda m: progress(0, total, m)) if progress else None)
    summaries: List[Summary] = []
    skipped: List[str] = []

    def notify(i: int, text: str) -> None:
        if progress:
            progress(i, total, text)

    for i, contact in enumerate(contacts, start=1):
        notify(i, f"正在提取「{contact.name}」的聊天记录…")
        try:
            messages = src.fetch(contact, start, end)
        except Exception as e:
            skipped.append(f"{contact.name}：提取失败（{e}）")
            notify(i, f"「{contact.name}」提取失败：{e}")
            continue

        bundle = ChatBundle(contact=contact, start=start, end=end, messages=messages)
        if bundle.count == 0:
            skipped.append(f"{contact.name}：该时间范围内没有聊天记录")
            notify(i, f"「{contact.name}」该时间范围内没有记录，已跳过")
            continue

        notify(i, f"正在生成「{contact.name}」的沟通总结（{bundle.count} 条消息）…")
        try:
            summaries.append(summarize(bundle, cfg))
        except Exception as e:
            skipped.append(f"{contact.name}：总结生成失败（{e}）")
            notify(i, f"「{contact.name}」总结生成失败：{e}")

    result = RunResult(summaries=summaries, skipped=skipped)

    if do_export and summaries:
        notify(total, "正在写入 Excel…")
        path, written, total_rows = exporter.export(
            summaries, cfg.get("export_path"), cfg.get("export_mode", "append")
        )
        result.export_path, result.written, result.total_rows = str(path), written, total_rows
        notify(total, f"已写入 {written} 行，文件共 {total_rows} 行数据")

    return result


def export_only(cfg: Dict[str, Any], summaries: Sequence[Summary]) -> RunResult:
    """界面上人工修订过总结后，单独触发导出。"""
    path, written, total_rows = exporter.export(
        summaries, cfg.get("export_path"), cfg.get("export_mode", "append")
    )
    return RunResult(summaries=list(summaries), skipped=[], export_path=str(path),
                     written=written, total_rows=total_rows)
