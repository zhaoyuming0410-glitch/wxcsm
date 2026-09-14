# -*- coding: utf-8 -*-
"""布局体检：用 Tk 自己报的几何数据检查界面有没有被挤出屏幕/压扁。

比截图更可靠的地方：它能给出精确像素，且能自动判定"底部按钮是否落在窗口内"——
这正是本文件注释里提到过的历史问题（高 DPI 下 ⑥ 结果区被推出可视区）。
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CFG = Path.home() / ".wxcsm" / "config.json"
BACKUP = CFG.read_text(encoding="utf-8") if CFG.exists() else None

import app as appmod

FAILS: list[str] = []
appmod.messagebox = type("MB", (), {
    "showinfo": lambda *a, **k: None, "showwarning": lambda *a, **k: None,
    "showerror": lambda *a, **k: None, "askyesno": lambda *a, **k: False})()


def note(ok: bool, msg: str) -> None:
    print(("  [OK]    " if ok else "  [!!]    ") + msg)
    if not ok:
        FAILS.append(msg)


def main() -> int:
    a = appmod.App()
    a.update_idletasks()
    a.update()

    W, H = a.winfo_width(), a.winfo_height()
    SW, SH = a.winfo_screenwidth(), a.winfo_screenheight()
    print(f"屏幕 {SW}x{SH}    窗口 {W}x{H}    (窗口不得超出屏幕)")
    note(W <= SW and H <= SH, f"窗口尺寸 {W}x{H} 在屏幕内")

    # 通用递归遍历：不假设控件树的层级（上一版就是因为假设层级而找错了对象）
    def walk(w, out=None):
        out = [] if out is None else out
        out.append(w)
        for c in w.winfo_children():
            walk(c, out)
        return out

    all_w = walk(a)
    labels = {}
    for w in all_w:
        if w.winfo_class() == "TLabelframe":
            try:
                labels[str(w.cget("text")).strip()] = w
            except Exception:                                      # noqa: BLE001
                pass

    print("\n【界面上的所有编号区块】")
    for txt, w in labels.items():
        top = w.winfo_rooty() - a.winfo_rooty()
        par = w.nametowidget(w.winfo_parent())
        ptop = par.winfo_rooty() - a.winfo_rooty()
        pbot = ptop + par.winfo_height()
        bot = top + w.winfo_height()
        print(f"  {w.winfo_width():5d}x{w.winfo_height():<4d} 顶={top:4d} "
              f"底={bot:4d}  父底={pbot:4d}  mapped={w.winfo_ismapped()}  {txt[:34]}")
        note(w.winfo_height() > 25, f"区块「{txt[:20]}」高度 {w.winfo_height()}px 合理")
        note(bot <= H, f"区块「{txt[:20]}」底部未超出窗口")
        # 关键：分区块不能超出父容器，否则底部内容会被裁掉（⑤ 的说明行就这样被切过）
        note(bot <= pbot + 1,
             f"区块「{txt[:20]}」底部 {bot} 未超出父容器底 {pbot}")

    # ⑥ 结果区
    res = next((w for t, w in labels.items() if "⑥" in t), None)
    if res is None:
        note(False, "找不到 ⑥ 结果区块")
    else:
        tv = next((w for w in walk(res) if w.winfo_class() == "Treeview"), None)
        assert tv is not None, "⑥ 里没有 Treeview"
        note(tv.winfo_height() >= 80,
             f"结果表格可视高度 {tv.winfo_height()}px（>=80）")
        note(tv.winfo_width() >= 400, f"结果表格宽度 {tv.winfo_width()}px（>=400）")
        for b in [w for w in walk(res) if w.winfo_class() == "TButton"]:
            by = b.winfo_rooty() - a.winfo_rooty()
            vis = b.winfo_ismapped() and b.winfo_height() > 10 and by + b.winfo_height() <= H
            note(vis, f"按钮「{b.cget('text')}」可见（y={by}, h={b.winfo_height()}）")

    # ④ 里的字数/阶段行不能被新增的单选挤掉
    print("\n【④ 里的字数与客户阶段行】")
    for name, w in (("总结字数说明", a.lbl_chars_end), ("客户阶段下拉", a.cbo_stage),
                    ("客户阶段说明", a.lbl_stage_hint), ("生成内容单选", a.ent_min)):
        note(w.winfo_ismapped() and w.winfo_height() > 5,
             f"{name} 可见（h={w.winfo_height()}）")

    # ④ 区是否变得过高（右侧列比左侧高就会顶掉底部）
    f4 = next((w for t, w in labels.items() if "④" in t), None)
    f3 = next((w for t, w in labels.items() if "③" in t), None)
    f5 = next((w for t, w in labels.items() if "⑤" in t), None)
    if f4 is not None and f3 is not None and f5 is not None:
        need = f3.winfo_height() + f4.winfo_height() + f5.winfo_height()
        print(f"\n【右侧三块合计】③{f3.winfo_height()} + ④{f4.winfo_height()} "
              f"+ ⑤{f5.winfo_height()} = {need}px，窗口高 {H}px（⑥ 还要占位）")
        note(need < H, f"右侧三块合计 {need}px 未超过窗口高 {H}px")

    # 我方成员输入区
    print("\n【我方成员输入区】")
    note(a.frm_self.winfo_ismapped(), "我方成员区已显示")
    note(a.frm_self.winfo_width() > 300, f"我方成员区宽度 {a.frm_self.winfo_width()}px")
    note(a.btn_self_pick.winfo_ismapped(), "「选择…」按钮可见")

    # 压力测试：把窗口压到最小尺寸，⑤ 仍不得溢出（minsize 保护生效）
    print("\n【压力测试：最小窗口 1040x720】")
    a.geometry("1040x720")
    a.update_idletasks()
    a.update()
    W2, H2 = a.winfo_width(), a.winfo_height()
    print(f"  实际 {W2}x{H2}")
    for name in ("③", "④", "⑤"):
        w = next((x for t, x in labels.items() if name in t), None)
        if w is None:
            continue
        top = w.winfo_rooty() - a.winfo_rooty()
        bot = top + w.winfo_height()
        par = w.nametowidget(w.winfo_parent())
        pbot = (par.winfo_rooty() - a.winfo_rooty()) + par.winfo_height()
        note(bot <= pbot + 1,
             f"最小窗口下 区块「{name}」底部 {bot} 未超出父容器底 {pbot}")
    res2 = next((x for t, x in labels.items() if "⑥" in t), None)
    if res2 is not None:
        tv2 = next((x for x in walk(res2) if x.winfo_class() == "Treeview"), None)
        rtop = res2.winfo_rooty() - a.winfo_rooty()
        note(rtop + res2.winfo_height() <= H2,
             f"最小窗口下 ⑥ 未超出窗口（底 {rtop + res2.winfo_height()} <= {H2}）")
        if tv2 is not None:
            # 最小窗口下结果表只剩几十像素是"能接受但很挤"：内容总量本来就超过
            # 720px 的窗口，只能由可滚动的结果区承担。要求 >=40px（约一行半），
            # 低于这个值说明布局退化到了不可用。
            note(tv2.winfo_height() >= 40,
                 f"最小窗口下 结果表格仍可用（{tv2.winfo_height()}px）")

    a.destroy()
    return 1 if FAILS else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        if BACKUP is not None:
            CFG.write_text(BACKUP, encoding="utf-8")
    print()
    print(f"布局体检失败 {len(FAILS)} 项" if FAILS else "布局体检全部通过")
    sys.exit(code)
