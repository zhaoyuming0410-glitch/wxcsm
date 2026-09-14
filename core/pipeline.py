"""业务流水线：GUI 与 CLI 共用同一套逻辑，避免两边行为不一致。

流程固定为四步：
    选数据源 → 取聊天对象 → 按时间范围抓消息 → 逐组生成结果 → 写 Excel

「一组聊天对象 + 时间范围」的产出取决于 output_format：
    summary —— 一篇叙述式沟通总结，对应「客户沟通总结」表的一行
    qa      —— 一组问答清单，对应「客户问题答复清单」表的多行
    both    —— 两者都生成（默认），写进同一个工作簿的两张表
抓消息只做一次，两种产出复用同一份 bundle，所以 both 的额外成本只在生成阶段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import exporter
from .models import ChatBundle, Contact, QaItem, Summary
from .qa_extract import extract_qa, summarize_kinds
from .sources import ChatSource, SourceError, create
from .summarizer import summarize

ProgressFn = Callable[[int, int, str], None]

# 输出形式（GUI 单选 / CLI --output-format 共用同一份定义，避免两边不一致）
OUTPUT_FORMATS: List[Tuple[str, str]] = [
    ("both", "两者都要（沟通总结 + 问题答复清单）"),
    ("summary", "仅沟通总结（一段叙述）"),
    ("qa", "仅问题答复清单（客户问题/我方答复表格）"),
]


def wants_summary(cfg: Dict[str, Any]) -> bool:
    return (cfg.get("output_format") or "both") in ("summary", "both")


def wants_qa(cfg: Dict[str, Any]) -> bool:
    return (cfg.get("output_format") or "both") in ("qa", "both")


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
    qa_items: List[QaItem] = field(default_factory=list)
    export_path: Optional[str] = None
    written: int = 0
    total_rows: int = 0
    qa_export_path: Optional[str] = None
    qa_written: int = 0
    qa_total_rows: int = 0
    qa_engine: str = ""            # 实际用了哪个引擎：ai / offline / ai+offline（混合）
    qa_notes: List[str] = field(default_factory=list)   # 降级原因等需要如实告知 user 的话

    @property
    def ok_count(self) -> int:
        return len(self.summaries)

    @property
    def qa_count(self) -> int:
        return len(self.qa_items)

    @property
    def qa_kinds(self) -> str:
        """如「报错 2 · 提问 5 · 我方待办 3」，给界面/CLI 的一句话统计。"""
        return summarize_kinds(self.qa_items)


# ---------- 问答清单引擎调度 ----------
def extract_qa_engine(bundle: ChatBundle, cfg: Dict[str, Any]
                      ) -> Tuple[List[QaItem], str, str]:
    """按 engine 配置选问答抽取引擎，AI 走不通就降级到离线。

    返回 (条目, 实际使用的引擎, 需要如实告知用户的说明)。
    降级策略与叙述式总结一致：`auto` 和 `ai` 都先试 AI——区别只在于
    `ai` 是用户明确点名要 AI，失败原因更要如实说出来，不能悄悄换掉。
    """
    engine = (cfg.get("engine") or "auto").lower()
    if engine in ("auto", "ai"):
        try:
            from .qa_ai import extract_qa_ai
            return extract_qa_ai(bundle, cfg), "ai", ""
        except Exception as e:  # noqa: BLE001 —— 任何失败都应降级，不能让功能整块失效
            return extract_qa(bundle, cfg), "offline", f"AI 不可用已自动降级到离线引擎（{e}）"
    return extract_qa(bundle, cfg), "offline", ""


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
    """对每个聊天对象抓取区间消息，按 output_format 生成总结和/或问答清单。"""
    if start > end:
        raise ValueError("开始日期不能晚于结束日期。")
    if not contacts:
        raise ValueError("请至少选择一个聊天对象。")

    total = len(contacts)
    # 前置步骤(取钥/解密)的进度: 进度条保持 0, 只更新状态栏文字, 避免误示处理已完成
    src = build_source(cfg, progress_cb=(lambda m: progress(0, total, m)) if progress else None)
    do_sum, do_qa = wants_summary(cfg), wants_qa(cfg)
    what = " 与 ".join(x for x, on in (("沟通总结", do_sum), ("问题答复清单", do_qa)) if on)
    summaries: List[Summary] = []
    qa_items: List[QaItem] = []
    skipped: List[str] = []
    qa_engines: set = set()
    qa_notes: List[str] = []

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

        notify(i, f"正在生成「{contact.name}」的{what}（{bundle.count} 条消息）…")
        # 两种产出的失败互不牵连：问答抽崩了不该把已经生成好的总结一起丢掉
        if do_sum:
            try:
                summaries.append(summarize(bundle, cfg))
            except Exception as e:
                skipped.append(f"{contact.name}：总结生成失败（{e}）")
                notify(i, f"「{contact.name}」总结生成失败：{e}")
        if do_qa:
            try:
                # 引擎选择与降级放在这里（而不是 qa_extract 里），
                # 否则 qa_ai → qa_extract 的依赖会变成循环导入
                items, eng, note = extract_qa_engine(bundle, cfg)
                qa_items.extend(items)
                qa_engines.add(eng)
                if note:
                    qa_notes.append(f"{contact.name}：{note}")
            except Exception as e:
                skipped.append(f"{contact.name}：问答清单生成失败（{e}）")
                notify(i, f"「{contact.name}」问答清单生成失败：{e}")

    result = RunResult(summaries=summaries, skipped=skipped, qa_items=qa_items,
                       qa_engine="+".join(sorted(qa_engines)) if qa_engines else "",
                       qa_notes=qa_notes)
    if do_export:
        _export_all(result, cfg, notify, total)
    return result


def _export_all(result: RunResult, cfg: Dict[str, Any],
                notify: Optional[Callable[[int, str], None]] = None,
                total: int = 0) -> None:
    """把本轮产出的表写盘。两张表各自判断、各自报告，一张失败不影响另一张。"""
    mode = cfg.get("export_mode", "append")

    def say(text: str) -> None:
        if notify:
            notify(total, text)

    if result.summaries:
        say("正在写入「客户沟通总结」…")
        path, written, total_rows = exporter.export(
            result.summaries, cfg.get("export_path"), mode)
        result.export_path, result.written, result.total_rows = str(path), written, total_rows
        say(f"沟通总结已写入 {written} 行，表内共 {total_rows} 行")
    if result.qa_items:
        say("正在写入「客户问题答复清单」…")
        path, written, total_rows = exporter.export_qa(
            result.qa_items, cfg.get("export_path"), mode)
        result.qa_export_path, result.qa_written, result.qa_total_rows = (
            str(path), written, total_rows)
        say(f"问题答复清单已写入 {written} 行，表内共 {total_rows} 行")


def export_results(cfg: Dict[str, Any], summaries: Sequence[Summary],
                   qa_items: Sequence[QaItem]) -> RunResult:
    """界面导出：按 output_format 把总结和/或问答清单写进同一个工作簿。

    界面上两张表的数据是一起累积的（用户可能先跑"仅总结"再跑"两者都要"），
    所以这里以当前选中的「输出形式」为准决定写哪张，和用户看到的单选一致，
    避免出现"我只想要总结，怎么还多了一张表"的意外。
    """
    res = RunResult(summaries=list(summaries) if wants_summary(cfg) else [],
                    skipped=[],
                    qa_items=list(qa_items) if wants_qa(cfg) else [])
    _export_all(res, cfg)
    return res


def export_only(cfg: Dict[str, Any], summaries: Sequence[Summary]) -> RunResult:
    """界面上人工修订过总结后，单独触发导出。"""
    path, written, total_rows = exporter.export(
        summaries, cfg.get("export_path"), cfg.get("export_mode", "append")
    )
    return RunResult(summaries=list(summaries), skipped=[], export_path=str(path),
                     written=written, total_rows=total_rows)


def export_qa_only(cfg: Dict[str, Any], items: Sequence[QaItem]) -> RunResult:
    """界面上人工修订过问答条目后，单独触发导出。"""
    path, written, total_rows = exporter.export_qa(
        items, cfg.get("export_path"), cfg.get("export_mode", "append")
    )
    return RunResult(summaries=[], skipped=[], qa_items=list(items),
                     qa_export_path=str(path), qa_written=written, qa_total_rows=total_rows)
