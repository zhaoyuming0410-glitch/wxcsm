# -*- coding: utf-8 -*-
"""真实数据 GUI 端到端：不点鼠标，但走的是界面自己的代码路径。

与 check_gui_smoke.py 的分工：
  check_gui_smoke  用假数据验证界面逻辑（快、可重复、不需要真实数据）
  本文件            用真实微信数据验证「界面 → pipeline → Excel」真的能跑通
关键点是**不绕开界面**：调用的都是按钮绑定的那几个方法。

需要真实数据与桌面会话，默认不跑。指定一个你本机确实存在的聊天对象名即可：
    python tests/check_gui_e2e_wx4.py --contact "某某客户交付群"

刻意不写默认对象名：仓库是公开的，不该把真实客户名写进代码。
"""
from __future__ import annotations

import argparse
import io
import sys
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tkinter as tk

CFG = Path.home() / ".wxcsm" / "config.json"
BACKUP = CFG.read_text(encoding="utf-8") if CFG.exists() else None

import app as appmod
from core import pipeline

FAILS: list[str] = []
OUT = Path.home() / "wxcsm-gui-验收"
CONTACT = ""      # 由 --contact 提供


class MB:
    """记录所有弹窗；askyesno 一律返回 False，避免自动打开 Excel。"""

    def __init__(self):
        self.calls = []

    def showinfo(self, *a, **k):
        self.calls.append(("info", a[0] if a else ""))

    def showwarning(self, *a, **k):
        self.calls.append(("warn", a[0] if a else ""))

    def showerror(self, *a, **k):
        self.calls.append(("error", a[0] if a else "", a[1] if len(a) > 1 else ""))

    def askyesno(self, *a, **k):
        self.calls.append(("ask", a[0] if a else ""))
        return False


def note(ok, msg):
    print(("  [OK]    " if ok else "  [!!]    ") + msg)
    if not ok:
        FAILS.append(msg)


def pump(a, seconds: float) -> None:
    """让 Tk 事件循环跑一会儿——后台线程的结果要靠 _drain_queue 取回来。"""
    t0 = time.time()
    while time.time() - t0 < seconds:
        a.update()
        time.sleep(0.05)


def main() -> int:
    mb = MB()
    appmod.messagebox = mb                                             # type: ignore
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / "GUI端到端.xlsx"
    if out.exists():
        out.unlink()

    a = appmod.App()
    a.update()

    print(f"【1】在界面里切换到 wx4 数据源并加载聊天对象")
    # 走到界面自己的下拉框逻辑：设值 → 触发 _on_source_change
    keys = [k for k, m in a._source_meta.items()]
    idx = keys.index("wx4")
    a.cbo_source.current(idx)
    a._on_source_change()
    a.update()
    note(a._source_key() == "wx4", f"数据源已切到 wx4（当前 {a._source_key()}）")

    a._load_contacts()          # 界面上「加载聊天对象」按钮绑的就是它
    pump(a, 25)
    note(bool(a.contacts), f"加载到 {len(a.contacts)} 个聊天对象")

    print(f"【2】勾选目标群聊：{CONTACT}")
    hit = [c for c in a.contacts if CONTACT in c.name]
    note(bool(hit), f"找到目标：{[c.name for c in hit]}")
    if not hit:
        a.destroy()
        return 1
    a.picked = {hit[0].cid}

    print("【3】在界面里设置时间范围（用③的预设按钮逻辑）")
    a._apply_preset("this_quarter")
    a.update()
    note(bool(a.var_start.get() and a.var_end.get()),
         f"时间范围 {a.var_start.get()} ~ {a.var_end.get()}")

    print("【4】在界面里选「两者都要」+「仅本地」（避免网络抖动影响验证）")
    a.var_format.set("both")
    a.var_engine.set("offline")
    a._update_format_ui()
    a.update()
    note(a._res_shape in ("summary", "qa"), f"结果区形态 {a._res_shape}")

    print("【5】把「我方成员」填成 张三（这是问答清单能不能配对的关键）")
    a.var_self_names.set("张三, 李四")
    a.update()

    print("【6】点「开始提取并生成总结」——走 _start 全链路")
    a.var_export.set(str(out))
    a.var_mode.set("overwrite")
    a._start()
    # 大群 725 条消息 + 离线抽取，给足时间；期间 Tk 事件循环持续跑
    pump(a, 180)
    errs = [c for c in mb.calls if c[0] == "error"]
    note(not errs, f"过程无错误弹窗（{errs if errs else '无'}）")

    print("【7】核对结果区内容")
    note(bool(a.summaries), f"结果区拿到 {len(a.summaries)} 篇总结")
    note(bool(a.qa_items), f"结果区拿到 {len(a.qa_items)} 条问答条目")
    if a.qa_items:
        kinds = {}
        for it in a.qa_items:
            kinds[it.kind] = kinds.get(it.kind, 0) + 1
        unanswered = sum(1 for it in a.qa_items if it.status == "未答复（待跟进）")
        print(f"          类型分布 {kinds}")
        print(f"          未答复 {unanswered} 条")
        # 填了 self_names 却不该是大面积未答复——这正是第②步修的核心 bug
        ratio = unanswered / len(a.qa_items)
        note(ratio < 0.6,
             f"未答复占比 {ratio:.0%}（<60% 说明「我方」识别生效，"
             f"历史上不填 self_names 时会超过 70%）")
        note(all(it.answer or it.kind == "我方待办" or
                 it.status == "未答复（待跟进）" for it in a.qa_items),
             "「已答复」的行都有答复正文")
        note(all((it.answer_time == "—") == (it.status != "已答复")
                 for it in a.qa_items), "答复时间与状态自洽")
    note(len(a.tv_res.get_children()) == len(a.qa_items),
         f"表格渲染 {len(a.tv_res.get_children())} 行 = 问答条目 {len(a.qa_items)} 条")

    print("【8】点「导出到 Excel」——走 _export 全链路")
    a._export()
    a.update()
    note(out.exists(), f"文件已生成：{out}")
    if out.exists():
        from openpyxl import load_workbook
        wb = load_workbook(out)
        note(set(wb.sheetnames) == {"客户沟通总结", "客户问题答复清单"},
             f"两张 sheet 都在：{wb.sheetnames}")
        qs = wb["客户问题答复清单"]
        n = qs.max_row - 1
        note(n == len(a.qa_items), f"问答 sheet {n} 行 = 界面上 {len(a.qa_items)} 条")
        note(qs.cell(1, 1).value == "客户名称" and qs.cell(1, 8).value == "原始记录",
             f"表头正确：{[c.value for c in qs[1]][:3]}…")
        ss = wb["客户沟通总结"]
        note(ss.max_row - 1 == len(a.summaries),
             f"总结 sheet {ss.max_row - 1} 行 = 界面上 {len(a.summaries)} 篇")

    print("【9】界面上的修订改动能带进 Excel（问答行编辑）")
    if a.qa_items:
        a.var_format.set("qa")
        a._update_format_ui()
        a.update()
        a.tv_res.selection_set("0")
        a.qa_items[0].question = "【界面改过】" + a.qa_items[0].question
        a.qa_items[0].status = "未答复（待跟进）"
        a.qa_items[0].answer = ""
        a.qa_items[0].answer_ts = None
        a._render_results()
        a.update()
        note(a.tv_res.item("0", "values")[3].startswith("【界面改过】"),
             "修订后表格内容已更新")
        a._export()
        a.update()
        from openpyxl import load_workbook
        wb = load_workbook(out)
        first = wb["客户问题答复清单"].cell(2, 4).value
        note(str(first).startswith("【界面改过】"),
             f"修订已写进 Excel：{str(first)[:24]}…")

    a.destroy()
    return 1 if FAILS else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="真实数据 GUI 端到端验证")
    ap.add_argument("--contact", required=True,
                    help="本机真实存在的聊天对象名（支持部分匹配），如「某某客户交付群」")
    ap.add_argument("--out", default=str(OUT / "GUI端到端.xlsx"))
    args = ap.parse_args()
    CONTACT = args.contact
    OUT = Path(args.out).parent
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        code = main()
    finally:
        if BACKUP is not None:
            CFG.write_text(BACKUP, encoding="utf-8")
            print("\n（已还原 config.json）")
    print()
    print(f"GUI 端到端失败 {len(FAILS)} 项：" + "、".join(FAILS) if FAILS
          else "GUI 端到端全部通过")
    sys.exit(code)
