# -*- coding: utf-8 -*-
"""名称库落盘检查：走真实 config.json + 真实 Excel，重开窗口后仍在。

（由 tests/ 下的检查脚本使用，不是 unittest 用例：它需要桌面会话，
并且会写真实配置文件——所以全程备份并还原 config.json。）

这是本特性的核心承诺（"存起来下次不用重输"），所以单独验一次：
不是看内存里的 dict，而是看**磁盘上的配置文件**。
"""
import atexit
import io
import json
import sys
import tempfile
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openpyxl import Workbook

CFG = Path.home() / ".wxcsm" / "config.json"
BACKUP = CFG.read_text(encoding="utf-8") if CFG.exists() else None


def _restore_cfg():
    """无论如何都要还原 config.json。

    这个脚本以前是在文件末尾还原的：中途抛异常就还原不了。导入现在是
    **以文件为准的替换语义**，一旦崩在这里，用户真实的同事名单会被测试用的
    花名册顶掉——所以改用 atexit，异常退出也还原。
    """
    if BACKUP is not None:
        CFG.write_text(BACKUP, encoding="utf-8")
        print("（已还原 config.json）")


atexit.register(_restore_cfg)

import tkinter as tk

import app as appmod


class _MB:
    """替掉 messagebox：导入会先弹「确认更新名称库」，模态框会把自动化挂住。"""

    def __init__(self):
        self.asked = []

    def showinfo(self, *a, **k):
        pass

    def showwarning(self, *a, **k):
        pass

    def showerror(self, *a, **k):
        pass

    def askyesno(self, *a, **k):
        return False

    def askokcancel(self, *a, **k):
        self.asked.append(a)
        return True


appmod.messagebox = _MB()                                           # type: ignore

FAILS = []


def note(ok, msg):
    print(("  [OK]    " if ok else "  [!!]    ") + msg)
    if not ok:
        FAILS.append(msg)


def find(root, cls, text=None):
    out, stack = [], [root]
    while stack:
        w = stack.pop()
        if isinstance(w, cls) and (text is None or
                                   str(w.cget("text") if "text" in w.keys() else "") == text):
            out.append(w)
        stack.extend(w.winfo_children())
    return out


def on_disk():
    return json.loads(CFG.read_text(encoding="utf-8"))


# 造一份真实花名册：含同事的真名 + 一堆不该进来的列
roster = Path(tempfile.mkdtemp(prefix="wxcsm_lib_")) / "同事名单.xlsx"
wb = Workbook()
ws = wb.active
ws.append(["序号", "部门", "员工姓名", "手机号", "入职日期"])
ws.append([1, "交付部", "张三", "13800138000", "2025-01-06"])
ws.append([2, "交付部", "李四", "13900139000", "2025-03-01"])
ws.append([3, "交付部", "李四 交付", None, "2025-04-02"])
ws.append([4, "研发部", "王五", "13700137000", "2025-05-03"])
wb.save(roster)

a = appmod.App()
a.update()

print("【1】起始状态")
print(f"        配置里的名称库：{a.cfg.get('self_name_library')}")
print(f"        当前选用：{a.var_self_names.get()}")

print("【2】打开「选择我方成员」，导入这份花名册")
a._open_self_picker()
a.update()
t = [w for w in a.winfo_children() if isinstance(w, tk.Toplevel)][-1]

real = appmod.filedialog.askopenfilename
appmod.filedialog.askopenfilename = lambda *x, **k: str(roster)
try:
    find(t, tk.ttk.Button, "导入 Excel…")[0].invoke()
    a.update()
finally:
    appmod.filedialog.askopenfilename = real

dlg = [w for w in t.winfo_children() if isinstance(w, tk.Toplevel)][-1]
combo = find(dlg, tk.ttk.Combobox)[0]
note("员工姓名" in combo.get(), f"自动猜中姓名列：{combo.get()}")
find(dlg, tk.ttk.Button, "导入这些名字")[0].invoke()
a.update()

tv = find(t, tk.ttk.Treeview)[0]
rowmap = {tv.item(i, "values")[1]: tv.item(i, "values") for i in tv.get_children()}
note("张三" in rowmap and "李四" in rowmap and "王五" in rowmap,
     f"导入的人进了列表：{sorted(rowmap)}")
bad = [x for x in ("研发部", "交付部", "13800138000", "序号") if x in rowmap]
note(not bad, f"非姓名列的内容没被导入（混进来的：{bad or '无'}）")

print("【3】关掉窗口（点确定），看磁盘上的配置文件")
find(t, tk.ttk.Button, "确定")[0].invoke()
a.update()

d = on_disk()
lib = d.get("self_name_library") or []
print(f"        磁盘上的名称库：{lib}")
note("张三" in lib and "李四" in lib and "王五" in lib,
     "导入的名字已落盘到 config.json")
note("研发部" not in lib and "13800138000" not in lib, "脏数据没落盘")

print("【4】重开一个界面（模拟下次启动），名称库应自动带出来")
a.destroy()
a2 = appmod.App()
a2.update()
note(sorted(a2.cfg.get("self_name_library") or []) == sorted(lib),
     f"新界面读到的名称库一致：{a2.cfg.get('self_name_library')}")

# 先只把「张三」设为本次选用，才能验证"库里有、但这次没勾"的人默认不勾。
# （上一步确定时把导入的名字一并勾上了，那是预期行为——导入即要用。）
a2.var_self_names.set("张三")

a2._open_self_picker()
a2.update()
t2 = [w for w in a2.winfo_children() if isinstance(w, tk.Toplevel)][-1]
tv2 = find(t2, tk.ttk.Treeview)[0]
vals = {v[1]: v for v in (tv2.item(i, "values") for i in tv2.get_children())}
note("王五" in vals, "库成员出现在勾选列表里")
note(vals.get("张三", ["", ""])[0] == "☑", f"本次选用的人默认勾上：{vals.get('张三')}")
note(vals.get("王五", ["", ""])[0] == "☐",
     f"库里有但这次没选的人默认不勾：{vals.get('王五')}")
note("名称库" in vals.get("王五", ["", "", "", "", ""])[4],
     "来源列标注为名称库")

print("【5】勾上库里的王五 + 手动加一个新同事，再确定")
for i in tv2.get_children():
    if tv2.item(i, "values")[1] == "王五":
        tv2.selection_set(i)
        break
tv2.event_generate("<Button-1>", when="now")
a2.update()
ent = find(t2, tk.ttk.Entry)[0]
ent.insert(0, "新同事小王")
ent.focus_force()
ent.event_generate("<Return>", when="now")
a2.update()
find(t2, tk.ttk.Button, "确定")[0].invoke()
a2.update()

d2 = on_disk()
note("王五" in (d2.get("self_names") or []),
     f"勾选生效，本次选用：{d2.get('self_names')}")
note("新同事小王" in (d2.get("self_name_library") or []),
     f"手动添加的已入库：{d2.get('self_name_library')}")
note("李四 交付" in (d2.get("self_name_library") or []),
     "库里有、这次没勾的成员没被删掉")

a2.destroy()
print()
print(f"名称库验证失败 {len(FAILS)} 项：" + "、".join(FAILS) if FAILS
      else "名称库落盘验证全部通过")

# config.json 的还原交给 atexit（见文件开头 _restore_cfg）
sys.exit(1 if FAILS else 0)
