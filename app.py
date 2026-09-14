# -*- coding: utf-8 -*-
"""绿泡泡聊天记录总结 —— 图形界面。

面向对象：完全没有编程与命令行经验的业务人员。
因此界面遵守三条铁律：
  1. 全流程单屏完成，不藏页签、不跳向导，步骤用 ①②③④ 明确编号；
  2. 任何报错都翻译成人话，并给出"下一步该干什么"；
  3. 关键操作可撤销、可预览、可人工修订后再导出。

一个刻意的设计：勾选状态与搜索框解耦。
  业务人员的真实操作是"搜张总→勾上→搜丙丁→再勾上→一起生成"。
  若沿用普通列表的多选，第二次搜索会清空第一次的勾选，用户会反复抓狂。
  所以这里用「勾选列 + 独立的已选集合」，搜索只影响显示，绝不影响已勾选的对象。
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
import traceback
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from datetime import datetime, date
import webbrowser

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import __version__, config, name_library, pipeline, workbuddy_models
from core.models import (
    QA_KINDS, QA_STATUS_DONE, QA_STATUS_PENDING, QA_STATUS_PROMISED, KIND_LABEL, Contact,
    QaItem, Summary,
)
from core.name_library import merge_names, read_columns, split_names
from core.self_discover import Candidate, suggest_self_members
from core.sources import SourceError, choices, REGISTRY
from core.wx4 import keyring, locate
from core.wx4.errors import KeyNotFoundError, WechatNotRunningError

APP_TITLE = "绿泡泡聊天记录总结"


def _build_tag() -> str:
    """标出当前跑的是打包版还是源码版。

    为什么需要：打包出来的 exe 是**某个时间点的快照**，改了源码不重新打包，
    双击 exe 看到的还是旧界面（而且旧包不会报错、不会提示，就是"功能不见了"）。
    标题栏上写清楚版本号和来源，这类问题一眼就能判断，不用再靠猜。
    """
    return "打包版" if getattr(sys, "frozen", False) else "源码版"


APP_TITLE_FULL = f"{APP_TITLE}  v{__version__}（{_build_tag()}）"
# 字体按平台自适配：Windows 用微软雅黑；macOS 用苹方（PingFang SC），缺失时回退系统默认
if sys.platform == "darwin":
    FONT = ("PingFang SC", 9)
    FONT_B = ("PingFang SC", 9, "bold")
    FONT_H = ("PingFang SC", 11, "bold")
else:
    FONT = ("微软雅黑", 9)
    FONT_B = ("微软雅黑", 9, "bold")
    FONT_H = ("微软雅黑", 11, "bold")
MARK_ON, MARK_OFF = "√", ""
CLR_HEAD = "#1F5FA5"
CLR_SEL = "#DCE9F7"
CLR_MUTE = "#6B7280"
CLR_OK = "#137333"
CLR_ERR = "#B3261E"
# 与 Excel 里的标色保持一致：未答复=红、我方待办=橙
CLR_WARN = "#C00000"
CLR_PROMISE = "#BF6000"
RENDER_LIMIT = 2000          # 列表单次渲染上限：超过则只显示前 N 条并提示用搜索缩小（避免万级数据卡死）


def parse_curl_config(text: str) -> tuple:
    """从粘贴的 cURL 命令里解析出 (base_url, model, api_key)；解析不到对应项返回空串。

    支持常见写法: 单/双引号、\\ 续行、-H/--header、-d/--data、URL 后可带 -X POST。
    """
    url = model = key = ""
    if not text:
        return url, model, key
    # 归一: 去掉续行反斜杠, 合并换行
    s = re.sub(r"\\\s*\n", " ", text)
    s = s.replace("\r", " ")
    # URL: 第一个 http(s)://... 直到空白或引号
    m = re.search(r"https?://[^\s'\"\\]+", s)
    if m:
        url = m.group(0).rstrip(",;")
    # Authorization: Bearer <key>（允许 -H/--header, 单双引号）
    m = re.search(r"""(?:-H|--header)\s+['"]?\s*Authorization:\s*Bearer\s+([A-Za-z0-9_\-\.]+)""",
                  s, re.I)
    if not m:
        m = re.search(r"""Authorization:\s*Bearer\s+([A-Za-z0-9_\-\.]+)""", s, re.I)
    if m:
        key = m.group(1)
    # model: -d/--data 里 JSON 的 "model": "xxx"
    m = re.search(r"""(?:-d|--data(?:-raw)?)\s+['"](.*?)['"](?=\s|$)""", s, re.S)
    body = m.group(1) if m else s
    m = re.search(r"""['"]?model['"]?\s*:\s*['"]([^'"]+)['"]""", body)
    if m:
        model = m.group(1)
    return url, model, key


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE_FULL)
        # 高 DPI 缩放下,固定 1180x830 会被 Windows 放大后超出可视区, 导致底部
        # "结果区/导出到Excel"被推出屏幕。开启 DPI 感知并自适应屏幕, 保证全屏可见。
        self._enable_dpi_awareness()
        self._set_adaptive_geometry()
        self.minsize(1040, 720)

        self.cfg: Dict = config.load()
        self.contacts: List[Contact] = []
        self.filtered: List[Contact] = []   # 当前搜索+筛选后的全集（用于计数）
        self.visible: List[Contact] = []    # 实际渲染的行（受 RENDER_LIMIT 限制）
        self.picked: set[str] = set()
        self.summaries: List[Summary] = []
        self.qa_items: List[QaItem] = []
        # 「我方成员」勾选缓存：名字 -> 是否勾选。跨次打开「选择我方成员」窗口时保留，
        # 免得用户挑了一轮、关掉再开又得重挑。
        self.self_candidates: List = []
        self.msg_q: "queue.Queue[tuple]" = queue.Queue()
        self.busy = False
        # 主列表 类型筛选 + 排序 状态 (_build_left 会创建 var_kind)
        self._sort_field: str = ""      # ""=默认(加载顺序) | name/kind/cnt/last
        self._sort_rev: bool = False

        self._source_meta = {c[0]: c for c in choices()}

        self._init_style()
        self._build_menu()
        self._build_ui()
        self._on_source_change()
        self._apply_preset("7d")
        self._drain_id: Optional[str] = self.after(120, self._drain_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # 挂在 <Destroy> 上而不是只写在 _on_close 里：销毁窗口的路径不止"点关闭"
        # 一条（程序内部也会 destroy），哪条路径都得把定时器撤掉。
        self.bind("<Destroy>", self._on_destroy)

    # ================= 窗口适配(高 DPI 缩放) =================
    @staticmethod
    def _enable_dpi_awareness() -> None:
        """让 Tk 按物理像素渲染, 避免 Windows 将窗口放大后超出屏幕。"""
        try:
            import ctypes
            # Win 8.1+: PER_MONITOR_DPI_AWARE / SYSTEM_DPI_AWARE
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                import ctypes
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

    def _set_adaptive_geometry(self) -> None:
        """窗口尺寸适配屏幕工作区: 不超屏幕, 居中, 保证底部("导出到Excel")可见。"""
        try:
            sw = self.winfo_screenwidth()
            # 粗算可用高度(减任务栏)
            sh = self.winfo_screenheight()
            # 需求尺寸
            want_w, want_h = 1180, 830
            # 超出屏幕则收缩(留边距)
            w = min(want_w, max(1040, sw - 40))
            h = min(want_h, max(720, sh - 80))
            x = max(0, (sw - w) // 2)
            y = max(0, (sh - h) // 2)
            self.geometry(f"{w}x{h}+{x}+{y}")
            # 若工作区高度太小(小屏/高缩放), 直接最大化确保全部可见
            if sh < 760:
                self.state("zoomed")
        except Exception:
            self.geometry("1180x830")

    # ================= 样式 =================
    def _init_style(self) -> None:
        st = ttk.Style()
        for theme in ("vista", "winnative", "clam"):
            if theme in st.theme_names():
                st.theme_use(theme)
                break
        st.configure(".", font=FONT)
        st.configure("TLabelframe.Label", font=FONT_B, foreground=CLR_HEAD)
        st.configure("Hint.TLabel", foreground=CLR_MUTE, font=(FONT[0], 8))
        st.configure("Head.TLabel", font=FONT_H, foreground=CLR_HEAD)
        st.configure("Go.TButton", font=FONT_H, padding=(10, 9))
        st.configure("Treeview", font=FONT, rowheight=25)
        st.configure("Treeview.Heading", font=FONT_B)

    def _build_menu(self) -> None:
        bar = tk.Menu(self)
        m1 = tk.Menu(bar, tearoff=0)
        m1.add_command(label="AI 总结设置…", command=self._open_ai_settings)
        m1.add_command(label="打开配置文件所在目录", command=self._open_cfg_dir)
        m1.add_separator()
        m1.add_command(label="退出", command=self._on_close)
        bar.add_cascade(label="设置", menu=m1)
        m2 = tk.Menu(bar, tearoff=0)
        m2.add_command(label="使用说明", command=self._show_help)
        m2.add_command(label="数据来源说明（重要）", command=self._show_source_help)
        m2.add_command(label="关于", command=self._show_about)
        bar.add_cascade(label="帮助", menu=m2)
        self.config(menu=bar)

    # ================= 布局 =================
    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=3)
        root.columnconfigure(1, weight=2)
        root.rowconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        self._build_left(root)
        self._build_right(root)
        self._build_action(root)
        self._build_result(root)
        # 关键：窗口比内容矮时，grid 会按 weight 压缩第 0 行，而右侧列的区块压不下去
        # 就会向下溢出、盖住执行行与结果区（实测 ⑤ 的底部越界过 75px）。
        # 给第 0 行设 minsize 后它不再被压到需求高度以下，缺口全部由第 2 行（结果区）
        # 承担——结果表格本来就是可滚动的，压缩它无害。
        self.update_idletasks()
        root.rowconfigure(0, minsize=self.frm_right.winfo_reqheight())
        self._update_format_ui()      # 结果区建好后再按当前输出形式刷一次可用状态

    # ---------- 左：数据源 + 聊天对象 ----------
    def _build_left(self, root) -> None:
        left = ttk.Frame(root)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)

        # ① 数据来源
        f1 = ttk.Labelframe(left, text=" ① 选择聊天记录来源 ", padding=10)
        f1.grid(row=0, column=0, sticky="ew")
        f1.columnconfigure(1, weight=1)

        self.var_source = tk.StringVar(value=self.cfg.get("source", "wx4"))
        self.cbo_source = ttk.Combobox(f1, state="readonly", width=52,
                                       values=[m[1] for m in self._source_meta.values()])
        self.cbo_source.grid(row=0, column=0, columnspan=3, sticky="ew")
        self.cbo_source.bind("<<ComboboxSelected>>", lambda _e: self._on_source_change())

        self.lbl_hint = ttk.Label(f1, style="Hint.TLabel", wraplength=520, justify="left")
        self.lbl_hint.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 4))

        self.lbl_path = ttk.Label(f1, text="数据路径：")
        self.var_path = tk.StringVar(value=self.cfg.get("source_path", ""))
        self.ent_path = ttk.Entry(f1, textvariable=self.var_path)
        self.btn_path = ttk.Button(f1, text="浏览…", width=8, command=self._pick_path)
        self.lbl_path.grid(row=2, column=0, sticky="w")
        self.ent_path.grid(row=2, column=1, sticky="ew", padx=4)
        self.btn_path.grid(row=2, column=2, sticky="e")

        # 我方成员：群聊里同事的发言也要算「我方」，否则问答清单的答复会大面积
        # 显示"未答复"；导入源（真实姓名场景无「我方」标记）与解密数据库源
        # （群聊 is_sender 偶尔归错人）也用它。
        self.frm_self = ttk.Frame(f1)
        self.frm_self.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self.frm_self.columnconfigure(1, weight=1)
        ttk.Label(self.frm_self, text="我方成员：").grid(row=0, column=0, sticky="w")
        self.var_self_names = tk.StringVar(value=", ".join(self.cfg.get("self_names") or []))
        ttk.Entry(self.frm_self, textvariable=self.var_self_names).grid(
            row=0, column=1, sticky="ew", padx=4)
        self.btn_self_pick = ttk.Button(self.frm_self, text="选择…", width=8,
                                        command=self._open_self_picker)
        self.btn_self_pick.grid(row=0, column=2, sticky="e")
        ttk.Label(
            self.frm_self,
            text="填你和同事的名字（逗号分隔）。群聊里不填会把同事的答复当成客户发言，"
                 "问答清单会大面积显示「未答复」。点「选择…」可从聊天记录里挑人。",
            style="Hint.TLabel", wraplength=520, justify="left").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(2, 0))

        bar = ttk.Frame(f1)
        bar.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Button(bar, text="加载聊天对象", command=self._load_contacts).pack(side="left")
        self.lbl_src_state = ttk.Label(bar, text="尚未加载", style="Hint.TLabel")
        self.lbl_src_state.pack(side="left", padx=8)

        # 全局进度条（加载/抓取密钥等操作时显示）
        self.frm_progress = ttk.Frame(f1)
        self.frm_progress.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(2, 0))
        self.progress = ttk.Progressbar(self.frm_progress, mode="indeterminate", length=520)
        self.progress.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.lbl_progress = ttk.Label(self.frm_progress, text="", style="Hint.TLabel")
        self.lbl_progress.grid(row=0, column=1, sticky="w")
        self.frm_progress.grid_remove()  # 默认隐藏

        # 微信 4.x 直读状态面板（仅 wx4 源显示）
        self.frm_wx4 = ttk.Frame(f1)
        self.frm_wx4.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.frm_wx4.columnconfigure(1, weight=1)
        self.lbl_wx4_status = ttk.Label(self.frm_wx4, text="", style="Hint.TLabel")
        self.lbl_wx4_status.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        self.btn_wx4_capture = ttk.Button(self.frm_wx4, text="抓取数据库密钥", command=self._wx4_capture_key)
        self.btn_wx4_capture.grid(row=1, column=0, sticky="w")
        self.lbl_wx4_key_state = ttk.Label(self.frm_wx4, text="", style="Hint.TLabel")
        self.lbl_wx4_key_state.grid(row=1, column=1, sticky="w", padx=8)
        self._wx4_refresh_status()
        self.frm_wx4.grid_remove()  # 默认隐藏，选择 wx4 时显示

        # ② 聊天对象
        f2 = ttk.Labelframe(left, text=" ② 勾选聊天对象（可搜索、可多选） ", padding=10)
        f2.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        f2.columnconfigure(0, weight=1)
        f2.rowconfigure(2, weight=1)

        srow = ttk.Frame(f2)
        srow.grid(row=0, column=0, sticky="ew")
        srow.columnconfigure(1, weight=1)
        ttk.Label(srow, text="搜索：").grid(row=0, column=0)
        self.var_search = tk.StringVar()
        ent = ttk.Entry(srow, textvariable=self.var_search)
        ent.grid(row=0, column=1, sticky="ew", padx=(0, 6))
        self.var_search.trace_add("write", lambda *_: self._refresh_list())
        self.var_only_picked = tk.BooleanVar(value=False)
        ttk.Checkbutton(srow, text="只看已勾选", variable=self.var_only_picked,
                        command=self._refresh_list).grid(row=0, column=2)
        ttk.Label(srow, text="类型：").grid(row=0, column=3, padx=(10, 2))
        self.var_kind = tk.StringVar(value="全部")
        ttk.Combobox(srow, textvariable=self.var_kind, width=6,
                     values=("全部", "好友", "群聊"), state="readonly"
                     ).grid(row=0, column=4)
        self.var_kind.trace_add("write", lambda *_: self._refresh_list())

        brow = ttk.Frame(f2)
        brow.grid(row=1, column=0, sticky="ew", pady=(6, 4))
        ttk.Button(brow, text="全选当前列表", width=13,
                   command=lambda: self._bulk(True)).pack(side="left")
        ttk.Button(brow, text="取消当前列表", width=13,
                   command=lambda: self._bulk(False)).pack(side="left", padx=4)
        ttk.Button(brow, text="清空全部勾选", width=13,
                   command=self._clear_pick).pack(side="left")
        ttk.Button(brow, text="独立窗口选择…", width=14,
                   command=self._open_picker).pack(side="left", padx=(14, 0))
        self.lbl_pick = ttk.Label(brow, text="已勾选 0 个", style="Hint.TLabel")
        self.lbl_pick.pack(side="right")

        wrap = ttk.Frame(f2)
        wrap.grid(row=2, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        cols = ("sel", "name", "kind", "cnt", "last")
        self.tv = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="none")
        for cid, text, w, anchor in (
            ("sel", "选", 34, "center"), ("name", "聊天对象名称", 236, "w"),
            ("kind", "类型", 52, "center"), ("cnt", "消息数", 62, "e"),
            ("last", "最近活跃", 96, "center"),
        ):
            # 名称/类型/消息数/最近活跃 列头可点击排序; "选" 列不排序。
            # 注意:command 传 None 会在部分 tkinter 版本报 'value for "-command" missing',
            # 故非排序列不传 command 参数。
            if cid == "sel":
                self.tv.heading(cid, text=text)
            else:
                self.tv.heading(cid, text=text,
                                command=(lambda c=cid: self._toggle_sort(c)))
            self.tv.column(cid, width=w, anchor=anchor,
                           stretch=(cid == "name"), minwidth=w)
        self.tv.tag_configure("on", background=CLR_SEL)
        self.tv.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tv.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.tv.configure(yscrollcommand=sb.set)
        self.tv.bind("<Button-1>", self._on_row_click)
        self.tv.bind("<space>", lambda _e: None)

        ttk.Label(f2, text="提示：点击任意一行即可勾选／取消。搜索不会清空已勾选的对象。",
                  style="Hint.TLabel").grid(row=3, column=0, sticky="w", pady=(4, 0))

    # ---------- 右：时间范围 + 生成 + 导出 ----------
    def _build_right(self, root) -> None:
        right = ttk.Frame(root)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        self.frm_right = right      # _build_ui 要拿它的需求高度给第 0 行设 minsize

        # ③ 时间范围
        f3 = ttk.Labelframe(right, text=" ③ 设置提取时间范围 ", padding=10)
        f3.grid(row=0, column=0, sticky="ew")
        f3.columnconfigure(1, weight=1)

        pf = ttk.Frame(f3)
        pf.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 8))
        for i, (key, label) in enumerate(pipeline.PRESETS):
            ttk.Button(pf, text=label, width=9,
                       command=lambda k=key: self._apply_preset(k)
                       ).grid(row=i // 3, column=i % 3, padx=2, pady=2, sticky="ew")
        for c in range(3):
            pf.columnconfigure(c, weight=1)

        self.var_start = tk.StringVar()
        self.var_end = tk.StringVar()
        # 起止日期并成一行：纵向空间要留给 ⑥ 结果区，实测每省一行表格就多 25px
        drow = ttk.Frame(f3)
        drow.grid(row=1, column=0, columnspan=4, sticky="w")
        ttk.Label(drow, text="开始：").pack(side="left")
        ttk.Entry(drow, textvariable=self.var_start, width=12).pack(side="left")
        ttk.Label(drow, text="　结束：").pack(side="left")
        ttk.Entry(drow, textvariable=self.var_end, width=12).pack(side="left")
        ttk.Label(f3, text="格式 2026-08-01，含首尾两天。",
                  style="Hint.TLabel", wraplength=460, justify="left"
                  ).grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # ④ 生成内容 + 生成方式
        # 布局上刻意把单选做成「一行选项 + 一行说明」：三个长单选标签堆叠会让本区
        # 高达 269px，把下面的 ⑤ 挤出分配高度（实测底部越界 75px，与执行行重叠）。
        # 说明随选择动态变化，信息一点没少，还给 ⑥ 结果区腾出了高度。
        f4 = ttk.Labelframe(right, text=" ④ 选择生成什么、怎么生成 ", padding=10)
        f4.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        f4.columnconfigure(1, weight=1)

        # 4.1 生成内容（输出形式）——两张表写进同一个 Excel 的两张 sheet，可一次跑完
        ttk.Label(f4, text="生成内容：").grid(row=0, column=0, sticky="nw")
        self.var_format = tk.StringVar(
            value=self.cfg.get("output_format", "both") or "both")
        fmt_box = ttk.Frame(f4)
        fmt_box.grid(row=0, column=1, sticky="w")
        self._FMT_SHORT = {"both": "两者都要", "summary": "仅沟通总结",
                           "qa": "仅问题答复清单"}
        self._FMT_HINT = {
            "both": "一次跑完：两张表写进同一个 Excel，互不影响。",
            "summary": "每个对象一段叙述式沟通总结。",
            "qa": "客户问题 / 我方答复对照表，含未答复与待跟进。",
        }
        for val, _long in pipeline.OUTPUT_FORMATS:
            ttk.Radiobutton(fmt_box, text=self._FMT_SHORT.get(val, _long), value=val,
                            variable=self.var_format, command=self._update_format_ui
                            ).pack(side="left", padx=(0, 12))
        self.lbl_fmt_hint = ttk.Label(f4, text="", style="Hint.TLabel",
                                      wraplength=480, justify="left")
        self.lbl_fmt_hint.grid(row=1, column=1, sticky="w")

        ttk.Separator(f4, orient="horizontal").grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(6, 5))

        # 4.2 生成方式（引擎）——只管文字怎么来，不管生成几张表
        ttk.Label(f4, text="生成方式：").grid(row=3, column=0, sticky="nw")
        self.var_engine = tk.StringVar(value=self.cfg.get("engine", "auto"))
        eng_box = ttk.Frame(f4)
        eng_box.grid(row=3, column=1, sticky="w")
        self._ENG_SHORT = {"auto": "智能", "ai": "仅 AI", "offline": "仅本地"}
        self._ENG_HINT = {
            "auto": "优先用 AI，不可用时自动改用本地，不中断。",
            "ai": "只用 AI，效果最好；需联网和 API Key。",
            "offline": "只用本地规则；不联网，记录不外发。",
        }
        for val in ("auto", "ai", "offline"):
            ttk.Radiobutton(eng_box, text=self._ENG_SHORT[val], value=val,
                            variable=self.var_engine,
                            command=self._update_engine_hint
                            ).pack(side="left", padx=(0, 12))
        self.lbl_eng_hint = ttk.Label(f4, text="", style="Hint.TLabel",
                                      wraplength=480, justify="left")
        self.lbl_eng_hint.grid(row=4, column=1, sticky="w")

        crow = ttk.Frame(f4)
        crow.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.lbl_chars = ttk.Label(crow, text="总结字数：")
        self.lbl_chars.pack(side="left")
        self.var_min = tk.StringVar(value=str(self.cfg.get("min_chars", 50)))
        self.var_max = tk.StringVar(value=str(self.cfg.get("max_chars", 200)))
        self.ent_min = ttk.Entry(crow, textvariable=self.var_min, width=5)
        self.ent_min.pack(side="left")
        self.lbl_chars_mid = ttk.Label(crow, text=" ~ ")
        self.lbl_chars_mid.pack(side="left")
        self.ent_max = ttk.Entry(crow, textvariable=self.var_max, width=5)
        self.ent_max.pack(side="left")
        self.lbl_chars_end = ttk.Label(crow, text=" 字")
        self.lbl_chars_end.pack(side="left")
        ttk.Button(crow, text="AI 设置…", command=self._open_ai_settings).pack(side="right")

        # 客户阶段（影响总结侧重与段落优先级；留空=按对话关键词自动推断）
        srow_stage = ttk.Frame(f4)
        srow_stage.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.lbl_stage = ttk.Label(srow_stage, text="客户阶段：")
        self.lbl_stage.pack(side="left")
        self.var_stage = tk.StringVar(value=self.cfg.get("customer_stage") or "自动推断")
        self.cbo_stage = ttk.Combobox(
            srow_stage, state="readonly", width=14,
            textvariable=self.var_stage,
            values=["自动推断", "续费期", "实施中", "新签客户", "稳定使用"])
        self.cbo_stage.pack(side="left", padx=(4, 0))
        self.lbl_stage_hint = ttk.Label(
            srow_stage, text="（续费期优先保留风险，稳定使用偏进展）", style="Hint.TLabel")
        self.lbl_stage_hint.pack(side="left", padx=6)

        # ⑤ 导出
        f5 = ttk.Labelframe(right, text=" ⑤ 设置 Excel 保存位置 ", padding=10)
        f5.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        f5.columnconfigure(0, weight=1)
        self.var_export = tk.StringVar(value=self.cfg.get("export_path", ""))
        ttk.Entry(f5, textvariable=self.var_export).grid(row=0, column=0, sticky="ew")
        ttk.Button(f5, text="另存为…", width=9, command=self._pick_export
                   ).grid(row=0, column=1, padx=(4, 0))
        self.var_mode = tk.StringVar(value=self.cfg.get("export_mode", "append"))
        mrow = ttk.Frame(f5)
        mrow.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Radiobutton(mrow, text="追加到已有文件（推荐）", value="append",
                        variable=self.var_mode).pack(side="left")
        ttk.Radiobutton(mrow, text="覆盖重建", value="overwrite",
                        variable=self.var_mode).pack(side="left", padx=(10, 0))
        ttk.Label(f5, text="总结写「客户沟通总结」表，问答清单写另一张表，互不覆盖。",
                  style="Hint.TLabel").grid(row=2, column=0, columnspan=2,
                                            sticky="w", pady=(4, 0))

    # ---------- 中：执行区 ----------
    def _build_action(self, root) -> None:
        f = ttk.Frame(root, padding=(0, 10, 0, 6))
        f.grid(row=1, column=0, columnspan=2, sticky="ew")
        f.columnconfigure(1, weight=1)
        self.btn_go = ttk.Button(f, text="开始提取并生成总结", style="Go.TButton",
                                 command=self._start)
        self.btn_go.grid(row=0, column=0, rowspan=2, sticky="w")
        self.pb = ttk.Progressbar(f, mode="determinate")
        self.pb.grid(row=0, column=1, sticky="ew", padx=12, pady=(2, 2))
        self.var_status = tk.StringVar(value="准备就绪。请按 ① → ② → ③ 顺序操作，然后点左侧按钮。")
        ttk.Label(f, textvariable=self.var_status, style="Hint.TLabel"
                  ).grid(row=1, column=1, sticky="w", padx=12)

    # ---------- 下：结果 ----------
    # 结果区的列不写死：生成内容不同（沟通总结 / 问题答复清单），要看的列完全不同。
    # 两套列定义放在这里，_render_results 按当前输出形式切换。
    SUMMARY_COLS = (
        ("name", "客户名称", 190, False), ("range", "时间范围", 170, False),
        ("content", "总结内容", 640, True), ("meta", "字数/方式", 110, False),
    )
    QA_COLS = (
        ("name", "客户名称", 150, False), ("kind", "类型", 62, False),
        ("atime", "提问时间", 92, False), ("question", "客户问题", 330, True),
        ("answer", "我方答复/处理", 380, True), ("antime", "答复时间", 92, False),
        ("status", "状态", 118, False), ("raw", "原始记录", 320, False),
    )

    def _build_result(self, root) -> None:
        f = ttk.Labelframe(
            root, text=" ⑥ 结果（双击任意一行可人工修订后再导出） ", padding=10)
        f.grid(row=2, column=0, columnspan=2, sticky="nsew")
        f.columnconfigure(0, weight=1)
        f.rowconfigure(0, weight=1)

        wrap = ttk.Frame(f)
        wrap.grid(row=0, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        # 问答清单有 8 列，窄屏放不下，配横向滚动条
        self.tv_res = ttk.Treeview(wrap, columns=[c[0] for c in self.SUMMARY_COLS],
                                   show="headings", selectmode="browse")
        self.tv_res.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tv_res.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.sb_x = ttk.Scrollbar(wrap, orient="horizontal", command=self.tv_res.xview)
        self.sb_x.grid(row=1, column=0, sticky="ew")
        self.tv_res.configure(yscrollcommand=sb.set, xscrollcommand=self.sb_x.set)
        self.tv_res.bind("<Double-1>", self._edit_row)

        brow = ttk.Frame(f)
        brow.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.btn_export = ttk.Button(brow, text="导出到 Excel", command=self._export)
        self.btn_export.pack(side="left")
        ttk.Button(brow, text="打开 Excel 文件", command=self._open_export).pack(side="left", padx=6)
        self.btn_edit = ttk.Button(brow, text="修订选中总结", command=self._edit_row)
        self.btn_edit.pack(side="left")
        ttk.Button(brow, text="清空结果", command=self._clear_res).pack(side="left", padx=6)
        self.lbl_res = ttk.Label(brow, text="暂无结果", style="Hint.TLabel")
        self.lbl_res.pack(side="right")
        # 必须在 brow 建好之后调：_apply_result_columns 会改 btn_edit 的文案
        self._apply_result_columns("both")

    def _result_shape(self) -> str:
        """当前结果区该按哪种形态展示：qa=问题答复清单，summary=沟通总结。"""
        fmt = self.var_format.get() if hasattr(self, "var_format") else "both"
        if fmt == "qa":
            return "qa"
        if fmt == "summary":
            return "summary"
        # 「两者都要」时：有问答条目就优先显示问答清单（信息量更大，也是新功能），
        # 两者都有时用户可以切回"仅沟通总结"再看叙述版
        return "qa" if self.qa_items else "summary"

    def _apply_result_columns(self, shape: str) -> None:
        cols = self.QA_COLS if shape == "qa" else self.SUMMARY_COLS
        self._res_shape = shape
        self.tv_res.configure(columns=[c[0] for c in cols])
        for cid, text, w, stretch in cols:
            self.tv_res.heading(cid, text=text)
            self.tv_res.column(cid, width=w, stretch=stretch, minwidth=60,
                               anchor="w" if cid in ("content", "question", "answer", "raw")
                               else "center")
        self.tv_res.tag_configure("pend", foreground=CLR_WARN)
        self.tv_res.tag_configure("promise", foreground=CLR_PROMISE)
        self.btn_edit.configure(
            text="修订选中条目" if shape == "qa" else "修订选中总结")

    # ================= 进度条 =================
    def _show_progress(self, text: str) -> None:
        self.frm_progress.grid()
        self.lbl_progress.configure(text=text)
        self.progress.start(10)

    def _hide_progress(self) -> None:
        self.progress.stop()
        self.frm_progress.grid_remove()

    # ================= 数据源交互 =================
    def _source_key(self) -> str:
        label = self.cbo_source.get()
        for key, meta in self._source_meta.items():
            if meta[1] == label:
                return key
        return "demo"

    def _on_source_change(self) -> None:
        want = self.var_source.get()
        keys = list(self._source_meta)
        if not self.cbo_source.get():
            self.cbo_source.set(self._source_meta.get(want, self._source_meta[keys[0]])[1])
        key = self._source_key()
        meta = self._source_meta[key]
        _, _, hint, needs_path, path_label = meta
        path_kind = REGISTRY[key].path_kind
        self.lbl_hint.configure(text=hint)
        self.lbl_path.configure(text=f"{path_label}：",
                                foreground="black" if needs_path else CLR_MUTE)
        if needs_path and path_kind == "url":
            # 网络地址：只填框、不弹文件选择；地址为空时预填该数据源的默认地址
            if not self.var_path.get().strip():
                self.var_path.set(REGISTRY[key].default_path)
            self.ent_path.configure(state="normal")
            self.btn_path.configure(state="disabled")
        elif needs_path:
            self.ent_path.configure(state="normal")
            self.btn_path.configure(state="normal")
        else:
            self.ent_path.configure(state="disabled")
            self.btn_path.configure(state="disabled")
        if key in ("import", "wxdb", "wx4"):
            self.frm_self.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        else:
            self.frm_self.grid_remove()
        # wx4 专属面板:显示微信状态与密钥抓取按钮
        if key == "wx4":
            self.frm_wx4.grid()
            self._wx4_refresh_status()
        else:
            self.frm_wx4.grid_remove()
        self.lbl_src_state.configure(text="尚未加载")

    # ---------- wx4 微信直读面板 ----------
    def _wx4_refresh_status(self) -> None:
        """异步刷新微信运行状态与密钥缓存状态(不阻塞 GUI)。"""
        self.lbl_wx4_status.configure(text="正在探测微信环境…", foreground=CLR_MUTE)
        self.lbl_wx4_key_state.configure(text="", foreground=CLR_MUTE)
        threading.Thread(target=self._wx4_do_refresh, daemon=True).start()

    def _wx4_do_refresh(self) -> None:
        """后台线程:探测微信环境并更新 UI(选中活跃账号,换账号后能感知)。"""
        try:
            # 强制刷新环境(换账号后 detect 的 60s 缓存会掩盖新账号)。
            env = locate.detect_wechat_env(force=True)
            # 账号选择规则与「加载聊天对象」(wx4_live._resolve_account) 保持一致:
            #   微信运行中 -> 选活跃(正在登录)账号; 否则 -> 有缓存密钥的账号。
            acc = None
            if env.accounts:
                if env.running:
                    acc = env.accounts[0]  # detect 已按活跃度排序, 首位即正在登录账号
                else:
                    for a in env.accounts:
                        if keyring.has_key(a.account):
                            acc = a
                            break
                    if acc is None:
                        acc = env.accounts[0]
            if acc:
                has_key = keyring.has_key(acc.account)
                if env.running:
                    wechat_state = "微信运行中 ✓"
                else:
                    wechat_state = "微信未运行"
                if has_key:
                    key_state = "密钥已缓存 ✓"
                    self._ui(self.lbl_wx4_key_state.configure,
                             text=key_state, foreground=CLR_OK)
                else:
                    key_state = "未抓取密钥"
                    self._ui(self.lbl_wx4_key_state.configure,
                             text=key_state, foreground=CLR_ERR)
                self._ui(self.lbl_wx4_status.configure,
                         text=f"账号: {acc.account} | {wechat_state}")
            else:
                self._ui(self.lbl_wx4_status.configure,
                         text="未检测到微信账号", foreground=CLR_ERR)
                self._ui(self.lbl_wx4_key_state.configure,
                         text="", foreground=CLR_ERR)
        except Exception as e:
            self._ui(self.lbl_wx4_status.configure,
                     text=f"检测失败: {e}", foreground=CLR_ERR)
            self._ui(self.lbl_wx4_key_state.configure,
                     text="", foreground=CLR_ERR)

    def _ui(self, fn, **kw) -> None:
        """从后台线程安全地更新界面控件。

        Tkinter 的 after 不是线程安全的：窗口已销毁、或主线程不在 mainloop 时，
        它会抛 RuntimeError / TclError（实测打开界面后切换数据源就会刷出
        「main thread is not in main loop」）。界面都没了就没有必要再更新它，
        所以这里统一吞掉——顺带也避免退出时刷一屏无意义的报错。
        """
        try:
            self.after(0, lambda: fn(**kw))
        except (RuntimeError, tk.TclError):
            pass

    def _wx4_capture_key(self) -> None:
        """抓取微信 4.x 数据库密钥（后台线程执行，不阻塞 GUI）。

        走 V4 只读内存扫描。**本工具不会启动、也不会关闭微信** ——
        密钥只存在于微信进程内存里，所以微信没运行时无法抓取，只能请用户自己打开。
        """
        try:
            env = locate.detect_wechat_env(force=True)
            if not env.accounts:
                messagebox.showerror("未找到账号", "未在本机找到已登录的微信 4.x 账号数据目录。")
                return

            # 微信运行中: V4 只读内存扫描（活跃账号密钥在内存, 秒级可取）。
            if env.running:
                self._show_progress("正在扫描微信进程内存(只读)…")
                threading.Thread(target=self._wx4_do_capture,
                                 args=(env, False),
                                 daemon=True).start()
                return

            # 微信未运行: 密钥不在内存里, 抓不到 —— 只能请用户自己打开。
            # 这里原先写的是「是否自动启动微信并抓取密钥？」，但本工具没有任何
            # 启动微信的代码（全仓唯一的 os.startfile 是用来打开导出的 Excel 的），
            # 点了「是」也不会有微信窗口弹出来，用户会一直等一个永远不出现的扫码界面。
            self._hide_progress()
            messagebox.showinfo(
                "微信当前未运行",
                "无法抓取密钥：密钥只存在于微信进程的内存里，微信没运行时取不到。\n\n"
                "请先打开微信并登录，然后回到这里点「加载聊天对象」重新抓取。\n\n"
                "（已经在别处抓过密钥的话不需要这一步 —— 缓存过密钥后即使微信"
                "已退出，也能离线解密读记录。）")
            return
        except Exception as e:
            self._hide_progress()
            messagebox.showerror("错误", f"抓取过程出错: {e}")

    def _wx4_do_capture(self, env, restart):
        """后台线程：依次尝试所有已检测账号，找到密钥即停。

        restart 只影响失败时的提示文案（它是 memkey.capture_key 的保留兼容参数，
        不会真的重启微信），所以这里不去动它。
        """
        from core.wx4.memkey import capture_key
        last_err = ""
        for acc in env.accounts:
            try:
                key = capture_key(acc, progress_cb=lambda s: self.var_status.set(s),
                                  restart=restart, timeout=240)
                keyring.save_key(acc.account, key,
                                 wechat_version=env.version or "",
                                 wechat_exe=env.exe_path or "")
                self.msg_q.put(("wx4_key_ok",
                    f"密钥抓取成功！\n账号: {acc.account}"))
                return
            except (KeyNotFoundError, WechatNotRunningError) as e:
                last_err = str(e)
                continue
            except Exception as e:
                last_err = f"抓取过程出错: {e}"
                continue
        if restart:
            self.msg_q.put(("wx4_key_err",
                f"又试了 {len(env.accounts)} 个账号仍未找到密钥。\n{last_err}"))
        else:
            # 首次扫描失败: 提示重启微信后重试（重启要用户自己做）。
            self.msg_q.put(("wx4_key_restart_hint", last_err))

    def _pick_path(self) -> None:
        from core.sources import REGISTRY
        key = self._source_key()
        if REGISTRY[key].path_kind == "url":
            return  # 网络地址没有「浏览」可言
        label = self._source_meta[key][4]
        if key in ("import", "wxdb"):
            # 导入源 / 解密数据库源：都优先选压缩包，也兼容手动粘贴的文件夹路径
            p = filedialog.askopenfilename(
                title=f"选择{label}（支持 .zip 压缩包）",
                filetypes=[("微信导出压缩包", "*.zip"), ("所有文件", "*.*")])
        elif REGISTRY[key].path_kind == "file":
            p = filedialog.askopenfilename(title=f"选择{label}")
        else:
            p = filedialog.askdirectory(title=f"选择{label}")
        if p:
            self.var_path.set(p)

    def _load_contacts(self) -> None:
        if self.busy:
            return
        self._sync_cfg()
        self._show_progress("正在加载聊天对象（首次加载需解密数据库，稍候）…")
        self.lbl_src_state.configure(text="正在加载…")
        self._set_busy(True)

        def work():
            try:
                items = pipeline.load_contacts(self.cfg,
                                               progress_cb=lambda s: self.var_status.set(s))
                self.msg_q.put(("contacts", items))
            except SourceError as e:
                self.msg_q.put(("error", ("加载聊天对象失败", str(e))))
            except Exception as e:
                self.msg_q.put(("error", ("加载聊天对象出错", f"{e}\n\n{traceback.format_exc(limit=2)}")))
            finally:
                self.msg_q.put(("done_load", None))

        threading.Thread(target=work, daemon=True).start()

    # ================= 列表渲染 =================
    # 类型筛选/排序 共用逻辑 ================
    @staticmethod
    def _kind_code(label: str) -> str:
        """「全部/好友/群聊」→ '' / 'friend' / 'group'。"""
        for code, lab in (("friend", "好友"), ("group", "群聊"), ("", "全部")):
            if label == lab:
                return code
        return ""

    def _apply_kind(self, contacts, kind_label) -> list:
        code = self._kind_code(kind_label)
        if not code:
            return list(contacts)
        return [c for c in contacts if c.kind == code]

    @staticmethod
    def _kind_label(kind) -> str:
        return KIND_LABEL.get(kind, KIND_LABEL.get("unknown", "未知"))

    @staticmethod
    def _sort_key(field: str, c: Contact):
        if field == "name":
            return (c.name or "").lower()
        if field == "kind":
            return KIND_LABEL.get(c.kind, "未知")
        if field == "cnt":
            return c.msg_count or 0
        if field == "last":
            return c.last_time or datetime.min
        return 0

    def _sort_contacts(self, contacts, field, reverse=False) -> list:
        if not field:
            return contacts
        return sorted(contacts, key=lambda c: self._sort_key(field, c),
                      reverse=reverse)

    def _toggle_sort(self, field: str) -> None:
        if self._sort_field == field:
            self._sort_rev = not self._sort_rev
        else:
            self._sort_field = field
            self._sort_rev = False
        self._refresh_list()

    def _refresh_list(self) -> None:
        kw = self.var_search.get().strip()
        filtered = pipeline.filter_contacts(self.contacts, kw)
        # 类型筛选
        filtered = self._apply_kind(filtered, self.var_kind.get())
        if self.var_only_picked.get():
            filtered = [c for c in filtered if c.cid in self.picked]
        # 排序(排序后再截断,使"前 N 条"按排序生效)
        filtered = self._sort_contacts(filtered, self._sort_field, self._sort_rev)
        self.filtered = filtered
        shown = filtered[:RENDER_LIMIT]
        self.visible = shown
        self.tv.delete(*self.tv.get_children())
        for c in shown:
            on = c.cid in self.picked
            self.tv.insert(
                "", "end", iid=c.cid,
                values=(MARK_ON if on else MARK_OFF, c.name,
                        self._kind_label(c.kind),
                        c.msg_count or "—",
                        c.last_time.strftime("%Y-%m-%d") if c.last_time else "—"),
                tags=("on",) if on else (),
            )
        # 列头排序箭头
        for cid, text in (("name", "聊天对象名称"), ("kind", "类型"),
                          ("cnt", "消息数"), ("last", "最近活跃")):
            arrow = " ▼" if (self._sort_field == cid and self._sort_rev) else \
                    " ▲" if self._sort_field == cid else ""
            self.tv.heading(cid, text=f"{text}{arrow}")
        self._update_pick_label()

    def _update_pick_label(self) -> None:
        total = len(self.filtered)
        shown = len(self.visible)
        if total > RENDER_LIMIT:
            extra = f"  ·  仅显示前 {shown} 个（共 {total} 个），输入搜索可缩小范围"
        else:
            extra = f"  ·  共 {total} 个"
        self.lbl_pick.configure(text=f"已勾选 {len(self.picked)} 个{extra}")

    def _on_row_click(self, event) -> None:
        if self.tv.identify_region(event.x, event.y) == "heading":
            return
        iid = self.tv.identify_row(event.y)
        if not iid:
            return
        self._toggle(iid)

    def _toggle(self, cid: str) -> None:
        if cid in self.picked:
            self.picked.discard(cid)
            self.tv.set(cid, "sel", MARK_OFF)
            self.tv.item(cid, tags=())
        else:
            self.picked.add(cid)
            self.tv.set(cid, "sel", MARK_ON)
            self.tv.item(cid, tags=("on",))
        self._update_pick_label()

    def _bulk(self, on: bool) -> None:
        for c in self.visible:
            if on:
                self.picked.add(c.cid)
            else:
                self.picked.discard(c.cid)
        self._refresh_list()

    def _clear_pick(self) -> None:
        self.picked.clear()
        self._refresh_list()

    # ================= 独立选择窗口 =================
    def _open_picker(self) -> None:
        if not self.contacts:
            messagebox.showinfo("请先加载", "请先在 ① 步点「加载聊天对象」，再打开独立窗口选择。")
            return
        win = tk.Toplevel(self)
        win.title("选择聊天对象（独立窗口 · 勾选实时生效）")
        win.geometry("1120x720")
        win.minsize(900, 560)
        win.transient(self)
        win.columnconfigure(0, weight=1)
        win.rowconfigure(1, weight=1)

        srow = ttk.Frame(win)
        srow.grid(row=0, column=0, sticky="ew", padx=12, pady=8)
        srow.columnconfigure(1, weight=1)
        ttk.Label(srow, text="搜索：").grid(row=0, column=0)
        p_search = tk.StringVar()
        ttk.Entry(srow, textvariable=p_search).grid(row=0, column=1, sticky="ew", padx=(0, 6))
        p_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(srow, text="只看已勾选", variable=p_only,
                        command=lambda: self._refresh_picker(win, p_search, p_only, p_tv, p_lbl)
                        ).grid(row=0, column=2)
        ttk.Label(srow, text="类型：").grid(row=0, column=3, padx=(10, 2))
        win._kind = tk.StringVar(value="全部")
        ttk.Combobox(srow, textvariable=win._kind, width=6,
                     values=("全部", "好友", "群聊"), state="readonly"
                     ).grid(row=0, column=4)
        win._kind.trace_add(
            "write",
            lambda *_: self._refresh_picker(win, p_search, p_only, p_tv, p_lbl))
        win._sort_field = ""      # 独立窗口排序状态(独立于主列表)
        win._sort_rev = False
        ttk.Label(srow, text="点列头排序", style="Hint.TLabel"
                  ).grid(row=0, column=5, padx=(8, 0))
        ttk.Label(srow, text="勾选实时生效，关闭窗口即应用。", style="Hint.TLabel"
                  ).grid(row=0, column=6, padx=(10, 0))

        wrap = ttk.Frame(win)
        wrap.grid(row=1, column=0, sticky="nsew", padx=12)
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        p_tv = ttk.Treeview(wrap, columns=("sel", "name", "kind", "cnt", "last"),
                            show="headings", selectmode="none")
        for cid, text, w, anchor in (
            ("sel", "选", 34, "center"), ("name", "聊天对象名称", 380, "w"),
            ("kind", "类型", 52, "center"), ("cnt", "消息数", 72, "e"),
            ("last", "最近活跃", 110, "center"),
        ):
            # command 传 None 会在部分 tkinter 版本报错,故 "选" 列不传 command。
            if cid == "sel":
                p_tv.heading(cid, text=text)
            else:
                p_tv.heading(cid, text=text, command=(
                    lambda c=cid: self._toggle_pick_sort(
                        win, c, p_search, p_only, p_tv, p_lbl)))
            p_tv.column(cid, width=w, anchor=anchor, stretch=(cid == "name"), minwidth=w)
        p_tv.tag_configure("on", background=CLR_SEL)
        p_tv.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=p_tv.yview)
        sb.grid(row=0, column=1, sticky="ns")
        p_tv.configure(yscrollcommand=sb.set)
        p_tv.bind("<Button-1>", lambda e: self._on_picker_click(e, p_tv, p_lbl))

        brow = ttk.Frame(win)
        brow.grid(row=2, column=0, sticky="ew", padx=12, pady=8)
        ttk.Button(brow, text="全选当前列表", width=13,
                   command=lambda: self._picker_bulk(p_tv, True, p_lbl)).pack(side="left")
        ttk.Button(brow, text="取消当前列表", width=13,
                   command=lambda: self._picker_bulk(p_tv, False, p_lbl)).pack(side="left", padx=4)
        ttk.Button(brow, text="清空全部勾选", width=13,
                   command=lambda: self._picker_clear(p_tv, p_lbl)).pack(side="left")
        p_lbl = ttk.Label(brow, text="", style="Hint.TLabel")
        p_lbl.pack(side="right")
        ttk.Button(brow, text="完成选择", width=12,
                   command=win.destroy).pack(side="left", padx=(16, 0))

        p_search.trace_add("write", lambda *_: self._refresh_picker(win, p_search, p_only, p_tv, p_lbl))
        win.bind("<Destroy>", lambda _e: self._refresh_list())
        self._refresh_picker(win, p_search, p_only, p_tv, p_lbl)
        win.focus_set()

    def _toggle_pick_sort(self, win, field, p_search, p_only, p_tv, p_lbl) -> None:
        if win._sort_field == field:
            win._sort_rev = not win._sort_rev
        else:
            win._sort_field = field
            win._sort_rev = False
        self._refresh_picker(win, p_search, p_only, p_tv, p_lbl)

    def _refresh_picker(self, win, p_search, p_only, p_tv, p_lbl) -> None:
        kw = p_search.get().strip()
        filtered = pipeline.filter_contacts(self.contacts, kw)
        # 类型筛选
        filtered = self._apply_kind(filtered, win._kind.get())
        if p_only.get():
            filtered = [c for c in filtered if c.cid in self.picked]
        # 排序(独立窗口独立排序状态)
        if win._sort_field:
            filtered = self._sort_contacts(filtered, win._sort_field, win._sort_rev)
        shown = filtered[:RENDER_LIMIT]
        p_tv.delete(*p_tv.get_children())
        for c in shown:
            on = c.cid in self.picked
            p_tv.insert("", "end", iid=c.cid,
                        values=(MARK_ON if on else MARK_OFF, c.name,
                                self._kind_label(c.kind),
                                c.msg_count or "—",
                                c.last_time.strftime("%Y-%m-%d") if c.last_time else "—"),
                        tags=("on",) if on else ())
        # 列头排序箭头(仅独立窗口的 4 个排序列)
        for cid, text in (("name", "聊天对象名称"), ("kind", "类型"),
                          ("cnt", "消息数"), ("last", "最近活跃")):
            arrow = " ▼" if (win._sort_field == cid and win._sort_rev) else \
                    " ▲" if win._sort_field == cid else ""
            p_tv.heading(cid, text=f"{text}{arrow}")
        total = len(filtered)
        if total > RENDER_LIMIT:
            extra = f"  ·  仅显示前 {len(shown)} 个（共 {total} 个），搜索可缩小范围"
        else:
            extra = f"  ·  共 {total} 个"
        p_lbl.configure(text=f"已勾选 {len(self.picked)} 个{extra}")

    def _on_picker_click(self, event, p_tv, p_lbl) -> None:
        if p_tv.identify_region(event.x, event.y) == "heading":
            return
        iid = p_tv.identify_row(event.y)
        if not iid:
            return
        self._picker_toggle(iid, p_tv, p_lbl)

    def _picker_toggle(self, cid: str, p_tv, p_lbl) -> None:
        if cid in self.picked:
            self.picked.discard(cid)
            p_tv.set(cid, "sel", MARK_OFF)
            p_tv.item(cid, tags=())
        else:
            self.picked.add(cid)
            p_tv.set(cid, "sel", MARK_ON)
            p_tv.item(cid, tags=("on",))
        self._update_pick_label()
        self._recount_picker(p_lbl)

    def _picker_bulk(self, p_tv, on: bool, p_lbl) -> None:
        for iid in p_tv.get_children():
            if on:
                self.picked.add(iid)
                p_tv.set(iid, "sel", MARK_ON)
                p_tv.item(iid, tags=("on",))
            else:
                self.picked.discard(iid)
                p_tv.set(iid, "sel", MARK_OFF)
                p_tv.item(iid, tags=())
        self._update_pick_label()
        self._recount_picker(p_lbl)

    def _picker_clear(self, p_tv, p_lbl) -> None:
        self.picked.clear()
        for iid in p_tv.get_children():
            p_tv.set(iid, "sel", MARK_OFF)
            p_tv.item(iid, tags=())
        self._update_pick_label()
        self._recount_picker(p_lbl)

    def _recount_picker(self, p_lbl) -> None:
        cur = p_lbl.cget("text")
        parts = cur.split("  ·", 1)
        tail = parts[1] if len(parts) > 1 else ""
        p_lbl.configure(text=f"已勾选 {len(self.picked)} 个  ·{tail}")

    # ================= 时间范围 =================
    def _apply_preset(self, key: str) -> None:
        s, e = pipeline.preset_range(key)
        self.var_start.set(s.isoformat())
        self.var_end.set(e.isoformat())

    # ================= 执行 =================
    def _sync_cfg(self) -> None:
        self.cfg.update(
            source=self._source_key(),
            source_path=self.var_path.get().strip(),
            self_names=[x.strip() for x in
                        self.var_self_names.get().strip().replace("，", ",").split(",")
                        if x.strip()],
            engine=self.var_engine.get(),
            output_format=self.var_format.get(),
            export_path=self.var_export.get().strip() or config.DEFAULTS["export_path"],
            export_mode=self.var_mode.get(),
        )
        for key, var, lo, hi, dft in (("min_chars", self.var_min, 20, 400, 50),
                                      ("max_chars", self.var_max, 30, 800, 200)):
            try:
                self.cfg[key] = max(lo, min(hi, int(var.get())))
            except ValueError:
                self.cfg[key] = dft
        if self.cfg["min_chars"] >= self.cfg["max_chars"]:
            self.cfg["min_chars"], self.cfg["max_chars"] = 50, 200
            self.var_min.set("50")
            self.var_max.set("200")
        stage = self.var_stage.get()
        self.cfg["customer_stage"] = "" if stage == "自动推断" else stage
        config.save(self.cfg)

    def _update_engine_hint(self) -> None:
        if hasattr(self, "lbl_eng_hint"):
            self.lbl_eng_hint.configure(
                text=self._ENG_HINT.get(self.var_engine.get(), ""))

    def _update_format_ui(self) -> None:
        """「仅问题答复清单」时，总结字数与客户阶段完全不影响结果，置灰并说明，
        免得用户填了却奇怪为什么没生效。同时刷新两个说明行。"""
        only_qa = self.var_format.get() == "qa"
        st = "disabled" if only_qa else "normal"
        self.ent_min.configure(state=st)
        self.ent_max.configure(state=st)
        self.cbo_stage.configure(state="disabled" if only_qa else "readonly")
        self.lbl_chars_end.configure(text=" 字（仅沟通总结用）" if only_qa else " 字")
        self.lbl_stage_hint.configure(
            text="（仅沟通总结用，问答清单不受影响）" if only_qa
            else "（续费期优先保留风险，稳定使用偏进展）")
        if hasattr(self, "lbl_fmt_hint"):
            self.lbl_fmt_hint.configure(
                text=self._FMT_HINT.get(self.var_format.get(), ""))
        self._update_engine_hint()
        if hasattr(self, "tv_res"):
            self._render_results()

    def _start(self) -> None:
        if self.busy:
            return
        self._sync_cfg()
        chosen = [c for c in self.contacts if c.cid in self.picked]
        if not chosen:
            messagebox.showwarning(
                "还没选聊天对象",
                "请先在第 ① 步点「加载聊天对象」，然后在第 ② 步勾选至少一个对象。")
            return
        try:
            start = pipeline.parse_day(self.var_start.get(), "开始日期")
            end = pipeline.parse_day(self.var_end.get(), "结束日期")
        except ValueError as e:
            messagebox.showerror("日期填写有误", str(e))
            return
        if start > end:
            messagebox.showerror("日期填写有误", "开始日期不能晚于结束日期。")
            return
        if self.cfg["engine"] == "ai" and not (self.cfg.get("llm_api_key") or "").strip():
            messagebox.showerror("缺少 AI 密钥",
                                 "当前选择了「仅用 AI 生成」，但还没填 API Key。\n"
                                 "请到「设置 → AI 总结设置」填写，或改选其他生成方式。")
            return

        self._set_busy(True)
        self.pb.configure(maximum=len(chosen), value=0)
        self.var_status.set(f"开始处理 {len(chosen)} 个聊天对象…")

        def work():
            try:
                res = pipeline.run(
                    self.cfg, chosen, start, end,
                    progress=lambda i, t, m: self.msg_q.put(("progress", (i, t, m))),
                    do_export=True,
                )
                self.msg_q.put(("result", res))
            except Exception as e:
                self.msg_q.put(("error", ("生成失败", f"{e}\n\n{traceback.format_exc(limit=2)}")))
            finally:
                self.msg_q.put(("done_run", None))

        threading.Thread(target=work, daemon=True).start()

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.btn_go.configure(state=state)
        self.btn_export.configure(state=state)

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, payload = self.msg_q.get_nowait()
                if kind == "contacts":
                    self.contacts = payload
                    self.picked = {c.cid for c in payload if c.cid in self.picked}
                    self._refresh_list()
                    self.lbl_src_state.configure(text=f"已加载 {len(payload)} 个聊天对象")
                    self.var_status.set(
                        f"已加载 {len(payload)} 个聊天对象。请在第 ② 步勾选需要总结的对象。")
                elif kind == "progress":
                    i, t, msg = payload
                    self.pb.configure(maximum=t, value=i)
                    self.var_status.set(f"[{i}/{t}] {msg}")
                elif kind == "result":
                    self._on_result(payload)
                elif kind == "error":
                    title, msg = payload
                    self._hide_progress()
                    self.var_status.set(f"{title}：{msg.splitlines()[0]}")
                    messagebox.showerror(title, msg)
                elif kind == "wx4_key_ok":
                    self._hide_progress()
                    self._wx4_refresh_status()
                    messagebox.showinfo("成功", payload)
                elif kind == "wx4_key_err":
                    self._hide_progress()
                    messagebox.showerror("抓取失败", payload)
                elif kind == "wx4_key_restart_hint":
                    # V4 只读扫描失败(非活跃账号/密钥不在内存)。
                    # 注意：本工具**不会**自己重启微信（memkey.capture_key 的 restart 参数
                    # 是保留兼容用的空参数）。所以这里只能请用户手动重启，然后重试扫描。
                    # 原先这里写的是「是否重启微信后重试？」，点了「是」其实只是再扫一次内存，
                    # 而内存状态没变（微信没重启），必然同样失败——那个承诺是空的。
                    self._hide_progress()
                    ret = messagebox.askyesno(
                        "未取到密钥",
                        f"未能在运行中的微信内存里找到密钥。\n{payload}\n\n"
                        "请手动重启微信后再试：\n"
                        "  1. 右键任务栏托盘里的微信图标 → 退出（要完全退出，不是关窗口）\n"
                        "  2. 重新打开微信并登录\n"
                        "  3. 回到这里点「是」，重新扫描一次\n\n"
                        "（本工具不会替你关闭微信。若刚登录的正是要提取的账号，"
                        "关掉微信反而不利于取钥——先点「是」再扫一次也值得一试。）")
                    if ret:
                        env = locate.detect_wechat_env(force=True)
                        self._show_progress("正在扫描微信进程内存(只读)…")
                        threading.Thread(target=self._wx4_do_capture,
                                         args=(env, True),
                                         daemon=True).start()
                elif kind in ("done_load", "done_run"):
                    self._set_busy(False)
                    self._hide_progress()
                    if kind == "done_load":
                        self.lbl_src_state.configure(
                            text=self.lbl_src_state.cget("text").replace("正在加载…", "加载结束"))
        except queue.Empty:
            pass
        # 窗口已销毁时不要再排下一次：Tk 会报
        # invalid command name "…_drain_queue"（关闭主窗口时能看到）。
        try:
            if self.winfo_exists():
                self._drain_id = self.after(120, self._drain_queue)
        except tk.TclError:
            pass

    def _on_result(self, res: pipeline.RunResult) -> None:
        self.summaries.extend(res.summaries)
        self.qa_items.extend(res.qa_items)
        self._render_results()
        self.pb.configure(value=self.pb.cget("maximum"))
        parts = []
        if res.summaries:
            parts.append(f"生成 {res.ok_count} 篇总结")
        if res.qa_items:
            parts.append(f"生成 {res.qa_count} 条问题答复（{res.qa_kinds}）")
        if not parts:
            parts.append("没有生成内容")
        if res.export_path:
            parts.append(f"总结已写入 {res.written} 行 → {res.export_path}")
        if res.qa_export_path:
            parts.append(f"问答清单已写入 {res.qa_written} 条")
        if res.skipped:
            parts.append(f"{len(res.skipped)} 个对象被跳过")
        self.var_status.set("；".join(parts))

        degraded = [s for s in res.summaries if s.error]
        detail = "；".join(parts)
        if res.skipped:
            detail += "\n\n被跳过的对象：\n" + "\n".join(f"· {x}" for x in res.skipped[:12])
        if degraded:
            detail += f"\n\n注意：{len(degraded)} 篇因 AI 不可用改用了本地总结。\n" \
                      f"原因示例：{degraded[0].error}"
        # 问答清单的降级也要说出来：用户选了 AI 却拿到离线结果，不说就是欺骗
        if res.qa_notes:
            detail += f"\n\n注意：{len(res.qa_notes)} 个对象的问答清单改用了本地引擎。\n" \
                      f"原因示例：{res.qa_notes[0]}"
        if res.summaries or res.qa_items:
            detail += "\n\n可在下方表格双击任意一行修订文字，改完点「导出到 Excel」再存一次。"
            messagebox.showinfo("处理完成", detail)
        else:
            messagebox.showwarning("没有生成任何内容", detail or "所选时间范围内没有找到聊天记录。")

    def _render_results(self) -> None:
        """按当前输出形式渲染结果表。两种形态的 iid 编号各自独立编号到对应列表。"""
        shape = self._result_shape()
        if shape != getattr(self, "_res_shape", None):
            self._apply_result_columns(shape)
        self.tv_res.delete(*self.tv_res.get_children())

        if shape == "qa":
            for i, it in enumerate(self.qa_items):
                tag = ""
                if it.status == QA_STATUS_PENDING:
                    tag = "pend"
                elif it.status == QA_STATUS_PROMISED:
                    tag = "promise"
                self.tv_res.insert(
                    "", "end", iid=str(i),
                    values=(it.customer_name, it.kind, it.ask_time, it.question_cell,
                            it.answer or "", it.answer_time, it.status, it.raw),
                    tags=(tag,) if tag else ())
            pend = sum(1 for x in self.qa_items if x.status == QA_STATUS_PENDING)
            promised = sum(1 for x in self.qa_items if x.status == QA_STATUS_PROMISED)
            self.lbl_res.configure(
                text=f"共 {len(self.qa_items)} 条问答"
                     f"（未答复 {pend}、我方待办 {promised}）"
                if self.qa_items else "暂无结果")
        else:
            for i, s in enumerate(self.summaries):
                tag = "ai" if s.engine == "ai" else "off"
                self.tv_res.insert("", "end", iid=str(i),
                                   values=(s.customer_name, s.time_range, s.content,
                                           f"{s.char_len} 字 / {'AI' if tag == 'ai' else '本地'}"))
            self.lbl_res.configure(
                text=f"共 {len(self.summaries)} 篇总结" if self.summaries else "暂无结果")

    # ================= 结果操作 =================
    def _current_index(self) -> Optional[int]:
        sel = self.tv_res.selection()
        if not sel:
            return None
        try:
            return int(sel[0])
        except ValueError:
            return None

    def _edit_row(self, _event=None) -> None:
        """双击/按钮的统一入口：按当前结果形态分派到对应的编辑器。"""
        if getattr(self, "_res_shape", "summary") == "qa":
            self._edit_qa()
        else:
            self._edit_summary()

    def _edit_summary(self, _event=None) -> None:
        idx = self._current_index()
        if idx is None:
            messagebox.showinfo("请先选一行", "请先在结果表格里点选一行，再点「修订选中总结」。")
            return
        s = self.summaries[idx]
        win = tk.Toplevel(self)
        win.title(f"修订总结 —— {s.customer_name}")
        win.geometry("640x420")
        win.transient(self)
        win.grab_set()
        ttk.Label(win, text=f"{s.customer_name}    {s.time_range}",
                  style="Head.TLabel").pack(anchor="w", padx=12, pady=(12, 6))
        txt = tk.Text(win, wrap="word", font=("微软雅黑", 10), height=12)
        txt.pack(fill="both", expand=True, padx=12)
        txt.insert("1.0", s.content)
        info = ttk.Label(win, text="", style="Hint.TLabel")
        info.pack(anchor="w", padx=12, pady=(4, 0))

        lo, hi = int(self.cfg.get("min_chars", 50)), int(self.cfg.get("max_chars", 200))

        def count(*_):
            n = len(txt.get("1.0", "end-1c").replace("\n", "").replace(" ", ""))
            ok = lo <= n <= hi
            info.configure(text=f"当前 {n} 字（建议 {lo}-{hi} 字）" + ("" if ok else "  ← 超出建议区间"),
                           foreground=CLR_MUTE if ok else "#B3261E")

        txt.bind("<KeyRelease>", count)
        count()

        row = ttk.Frame(win)
        row.pack(fill="x", padx=12, pady=10)

        def save():
            self.summaries[idx].content = txt.get("1.0", "end-1c").strip()
            self._render_results()
            win.destroy()

        ttk.Button(row, text="保存修订", command=save).pack(side="right")
        ttk.Button(row, text="取消", command=win.destroy).pack(side="right", padx=6)

    # ---------- 问答条目编辑器 ----------
    @staticmethod
    def _parse_hm(text: str, year: int) -> Optional[datetime]:
        """解析「MM-DD HH:MM」并补上年份。无效返回 None（不抛异常，由调用方提示）。"""
        t = (text or "").strip().replace("/", "-")
        if not t:
            return None
        for fmt in ("%m-%d %H:%M", "%m-%d %H:%M:%S"):
            try:
                d = datetime.strptime(t, fmt)
            except ValueError:
                continue
            return d.replace(year=year)
        return None

    def _edit_qa(self, _event=None) -> None:
        idx = self._current_index()
        if idx is None or idx >= len(self.qa_items):
            messagebox.showinfo("请先选一行", "请先在结果表格里点选一行，再点「修订选中条目」。")
            return
        it = self.qa_items[idx]
        win = tk.Toplevel(self)
        win.title(f"修订问答条目 —— {it.customer_name}")
        win.geometry("720x620")
        win.transient(self)
        win.grab_set()
        ttk.Label(win, text=f"{it.customer_name}    {it.kind}    {it.status}",
                  style="Head.TLabel").pack(anchor="w", padx=12, pady=(12, 6))

        # 类型 / 状态 / 提问时间
        top = ttk.Frame(win)
        top.pack(fill="x", padx=12)
        ttk.Label(top, text="类型：").pack(side="left")
        var_kind = tk.StringVar(value=it.kind)
        ttk.Combobox(top, textvariable=var_kind, state="readonly", width=10,
                     values=list(QA_KINDS)).pack(side="left")
        ttk.Label(top, text="　提问时间：").pack(side="left")
        var_atime = tk.StringVar(value=it.ask_time)
        ttk.Entry(top, textvariable=var_atime, width=13).pack(side="left")
        ttk.Label(top, text="　状态：").pack(side="left")
        var_status = tk.StringVar(value=it.status)
        ttk.Combobox(top, textvariable=var_status, state="readonly", width=16,
                     values=[QA_STATUS_DONE, QA_STATUS_PENDING,
                             QA_STATUS_PROMISED]).pack(side="left")
        ttk.Label(win, text="时间格式 MM-DD HH:MM；「我方待办」行的提问时间可留空。",
                  style="Hint.TLabel").pack(anchor="w", padx=12, pady=(4, 0))

        ttk.Label(win, text="客户问题（「我方待办」行留空即可）：").pack(
            anchor="w", padx=12, pady=(10, 2))
        q_txt = tk.Text(win, wrap="word", font=("微软雅黑", 10), height=4)
        q_txt.pack(fill="x", padx=12)
        q_txt.insert("1.0", "" if it.question == "—" else it.question)

        ttk.Label(win, text="我方答复/处理：").pack(anchor="w", padx=12, pady=(10, 2))
        a_txt = tk.Text(win, wrap="word", font=("微软雅黑", 10), height=5)
        a_txt.pack(fill="x", padx=12)
        a_txt.insert("1.0", it.answer or "")

        ttk.Label(win, text="原始记录（只读，供核对原话）：").pack(
            anchor="w", padx=12, pady=(10, 2))
        raw = tk.Text(win, wrap="word", font=("微软雅黑", 9), height=4,
                      background="#F5F6F8")
        raw.pack(fill="both", expand=True, padx=12)
        raw.insert("1.0", it.raw)
        raw.configure(state="disabled")

        row = ttk.Frame(win)
        row.pack(fill="x", padx=12, pady=10)

        def save():
            at = self._parse_hm(var_atime.get(), (it.ask_ts or datetime.now()).year)
            if var_atime.get().strip() and at is None:
                messagebox.showerror("提问时间格式不对",
                                     "请按 MM-DD HH:MM 填写，例如 08-13 09:19。")
                return
            it.kind = var_kind.get()
            it.status = var_status.get()
            it.question = q_txt.get("1.0", "end-1c").strip()
            it.answer = a_txt.get("1.0", "end-1c").strip()
            if at is not None:
                it.ask_ts = at
            # 状态与时间必须自洽：说"已答复"就必须有答复正文，
            # 标成"未答复/待跟进"就不能留着会让人以为已闭环的答复时间。
            if it.kind == "我方待办":
                it.question = ""
                it.answer_ts = None
            elif it.status == QA_STATUS_DONE:
                if not it.answer:
                    messagebox.showerror(
                        "缺答复内容", "状态是「已答复」时必须填写我方答复/处理。\n"
                                     "如果其实没回，请把状态改成「未答复（待跟进）」。")
                    return
                if it.answer_ts is None:
                    it.answer_ts = it.ask_ts
            else:
                it.answer_ts = None
            self._render_results()
            win.destroy()

        ttk.Button(row, text="保存修订", command=save).pack(side="right")
        ttk.Button(row, text="取消", command=win.destroy).pack(side="right", padx=6)

    def _export(self) -> None:
        fmt = self.var_format.get()
        want_s = fmt in ("both", "summary")
        want_q = fmt in ("both", "qa")
        if (want_s and not self.summaries) and (want_q and not self.qa_items):
            messagebox.showinfo("没有可导出的内容", "请先生成内容，再执行导出。")
            return
        self._sync_cfg()
        try:
            res = pipeline.export_results(self.cfg, self.summaries, self.qa_items)
        except PermissionError as e:
            messagebox.showerror("文件被占用", str(e))
            return
        except Exception as e:
            messagebox.showerror("导出失败", str(e))
            return
        parts, lines = [], []
        if res.export_path:
            parts.append(f"总结 {res.written} 行")
            lines.append(f"「客户沟通总结」写入 {res.written} 行，表内共 {res.total_rows} 行")
        if res.qa_export_path:
            parts.append(f"问答清单 {res.qa_written} 条")
            lines.append(f"「客户问题答复清单」写入 {res.qa_written} 条，"
                         f"表内共 {res.qa_total_rows} 条")
        path = res.qa_export_path or res.export_path
        self.var_status.set("已导出：" + "、".join(parts) + f" → {path}")
        if messagebox.askyesno(
                "导出完成",
                "、".join(parts) + "。\n\n" + "\n".join(lines) + f"\n\n{path}\n\n现在打开这个文件吗？"):
            self._open_file(path)

    def _open_export(self) -> None:
        p = self.var_export.get().strip()
        if not p or not Path(p).exists():
            messagebox.showinfo("文件还不存在", "还没有生成过 Excel 文件，或路径已变更。")
            return
        self._open_file(p)

    @staticmethod
    def _open_file(path: str) -> None:
        try:
            if os.name == "nt":
                os.startfile(path)                       # noqa: S606
            elif sys.platform == "darwin":
                subprocess.run(["open", path], check=False)
            else:
                subprocess.run(["xdg-open", path], check=False)
        except Exception as e:
            messagebox.showerror("打不开文件", f"请手动打开：\n{path}\n\n({e})")

    def _clear_res(self) -> None:
        if (self.summaries or self.qa_items) and not messagebox.askyesno(
                "确认清空", "只清空界面上的结果列表，已导出的 Excel 文件不受影响。继续吗？"):
            return
        self.summaries.clear()
        self.qa_items.clear()
        self._render_results()

    def _pick_export(self) -> None:
        p = filedialog.asksaveasfilename(
            title="选择 Excel 保存位置", defaultextension=".xlsx",
            filetypes=[("Excel 工作簿", "*.xlsx")],
            initialfile=Path(self.var_export.get() or "微信客户沟通总结.xlsx").name)
        if p:
            self.var_export.set(p)

    # ================= 我方成员 / 名称库 =================
    def _self_library(self) -> List[str]:
        """取名称库。已配置的 self_names 自动并进来——
        这样老配置（只有 self_names、还没有库）第一次打开窗口时，
        现有名字就自动入了库，不需要额外的迁移步骤。"""
        return merge_names(self.cfg.get("self_name_library") or [],
                           split_names(self.var_self_names.get()))

    def _open_self_picker(self) -> None:
        """列出「可能是我方同事」的人，供勾选，并把认过的人存进名称库。

        名单有三条来源，合并进同一张表：
          1) 名称库：以前存下来的同事，本次是否使用由勾选决定；
          2) 已配置的 self_names：默认勾上——这样配错了也能取消勾选；
          3) 扫描：从已勾选的聊天对象里按行为证据找出新候选，默认不勾。
        只给候选、不替用户决定：判定"谁是同事"是业务问题，猜错会把客户的话
        当成我方答复，比漏掉更难发现。

        库与选用是两件事：取消勾选只是"这次不用"，不会把人从库里删掉；
        要真删得点「从名称库删除」。
        """
        win = tk.Toplevel(self)
        win.title("选择我方成员")
        win.geometry("900x640")
        win.minsize(780, 520)
        win.transient(self)
        win.grab_set()

        ttk.Label(win, text="我方成员（同事也算）", style="Head.TLabel").pack(
            anchor="w", padx=12, pady=(12, 4))
        ttk.Label(
            win,
            text="群聊里同事的发言也要算作「我方」，否则他们的答复会被当成客户发言，"
                 "问答清单会大面积显示「未答复」。\n"
                 "带 ☑ 的是本次要用的成员。名称库里的同事长期存着，不勾就只是这次不用，"
                 "不会被删掉；点「确定」时会把本次勾选的人都存进库。",
            style="Hint.TLabel", justify="left", wraplength=850).pack(anchor="w", padx=12)

        picked = [c for c in self.contacts if c.cid in self.picked]
        try:
            start = pipeline.parse_day(self.var_start.get(), "开始日期")
            end = pipeline.parse_day(self.var_end.get(), "结束日期")
        except ValueError:
            start = end = None

        # 名称库（长期存）
        library: List[str] = self._self_library()
        # 勾选状态：名字 -> 是否勾选。预填写 = 已配置的一律先勾上。
        state: Dict[str, bool] = {}
        for n in split_names(self.var_self_names.get()):
            state[n] = True
        # 候选项：名字 -> Candidate（同一名字保留评分更高的那条）
        rows: Dict[str, Candidate] = {}
        for n in library:
            rows[n] = Candidate(name=n, cid="（名称库）", reasons=["名称库"])
        for n in state:
            rows.setdefault(n, Candidate(name=n, cid="（已配置）", reasons=["已配置"]))

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=12, pady=(8, 4))
        ttk.Label(
            bar,
            text=(f"将扫描已勾选的 {len(picked)} 个聊天对象" if picked
                  else "尚未勾选聊天对象（请先在第 ① ② 步加载并勾选）"),
            style="Hint.TLabel").pack(side="left")
        var_state = tk.StringVar(value="")
        btn_scan = ttk.Button(bar, text="开始扫描", width=10)
        btn_scan.pack(side="left", padx=8)
        ttk.Label(bar, textvariable=var_state, style="Hint.TLabel").pack(side="left")

        addrow = ttk.Frame(win)
        addrow.pack(fill="x", padx=12, pady=(2, 4))
        ttk.Label(addrow, text="手动添加：").pack(side="left")
        var_new = tk.StringVar()
        ent_new = ttk.Entry(addrow, textvariable=var_new, width=20)
        ent_new.pack(side="left", padx=(4, 0))
        # command 稍后再接：add_manual 定义在后面（和 btn_scan 同样的写法）
        btn_add = ttk.Button(addrow, text="添加", width=6)
        btn_add.pack(side="left", padx=(6, 0))
        ttk.Label(addrow, text="（输名字后回车或点「添加」，会存进名称库）",
                  style="Hint.TLabel").pack(side="left", padx=6)

        librow = ttk.Frame(win)
        librow.pack(fill="x", padx=12, pady=(0, 6))
        var_lib = tk.StringVar(value=f"名称库：{len(library)} 人")
        ttk.Label(librow, textvariable=var_lib, style="Hint.TLabel").pack(side="left")

        def call_import() -> None:
            """从 Excel 导入姓名：先让用户确认是哪一列，再入库。"""
            p = filedialog.askopenfilename(
                title="选择含我方成员姓名的 Excel", parent=win,
                filetypes=[("Excel 工作簿", "*.xlsx *.xlsm"), ("所有文件", "*.*")])
            if not p:
                return
            try:
                cols = read_columns(p)
            except Exception as e:                                   # noqa: BLE001
                messagebox.showerror("读取失败",
                                     f"打不开这个文件：{e}\n\n"
                                     "请确认它是 .xlsx 工作簿，且没有被其他程序占用。",
                                     parent=win)
                return
            # 只保留清洗后真有名字的列
            usable = [c for c in cols if c.names]
            if not usable:
                messagebox.showwarning(
                    "没找到姓名",
                    "这个文件里没有读得出姓名的列。\n\n"
                    "可能是：内容在另一个 sheet、姓名被合并单元格包着，"
                    "或者表里只有数字。\n可以先用「手动添加」。", parent=win)
                return
            ask_import_column(win, p, usable)

        def ask_import_column(parent, path: str, usable: list) -> None:
            """让用户挑列并预览——员工表里序号/部门/手机号列都很像，不能猜。"""
            dlg = tk.Toplevel(parent)
            dlg.title("选择姓名列")
            dlg.geometry("560x580")
            dlg.transient(parent)
            dlg.grab_set()

            ttk.Label(dlg, text=f"从「{Path(path).name}」导入姓名",
                      style="Head.TLabel").pack(anchor="w", padx=12, pady=(12, 2))
            guess = name_library.guess_column(usable)
            var_col = tk.StringVar(
                value=(guess.label if guess else usable[0].label))
            ttk.Label(dlg, text="请选择姓名所在的列：", style="Hint.TLabel").pack(
                anchor="w", padx=12)
            cbo = ttk.Combobox(dlg, textvariable=var_col, state="readonly",
                               values=[c.label for c in usable], width=48)
            cbo.pack(anchor="w", padx=12, pady=(2, 8))

            ttk.Label(dlg, text="将要导入的名字（可上下滚动核对）：",
                      style="Hint.TLabel").pack(anchor="w", padx=12)
            wrap = ttk.Frame(dlg)
            wrap.pack(fill="both", expand=True, padx=12, pady=(2, 6))
            wrap.columnconfigure(0, weight=1)
            wrap.rowconfigure(0, weight=1)
            tv2 = ttk.Treeview(wrap, show="headings", columns=("n",), height=10)
            tv2.heading("n", text="名字")
            tv2.column("n", width=200, anchor="w")
            tv2.grid(row=0, column=0, sticky="nsew")
            sb2 = ttk.Scrollbar(wrap, orient="vertical", command=tv2.yview)
            sb2.grid(row=0, column=1, sticky="ns")
            tv2.configure(yscrollcommand=sb2.set)

            var_cnt = tk.StringVar()

            def by_label(label: str):
                return next(c for c in usable if c.label == label)

            def redraw(*_a) -> None:
                col = by_label(var_col.get())
                tv2.delete(*tv2.get_children())
                for i, n in enumerate(col.names):
                    tv2.insert("", "end", iid=str(i), values=(n,))
                var_cnt.set(f"共 {len(col.names)} 个名字")

            cbo.bind("<<ComboboxSelected>>", redraw)
            redraw()
            ttk.Label(dlg, textvariable=var_cnt, style="Hint.TLabel").pack(
                anchor="w", padx=12)
            ttk.Label(dlg, style="Hint.TLabel", justify="left", wraplength=520,
                      text="导入会按这个文件更新名称库：文件里的名字进库，"
                           "库里不在文件里的人会被移除——点「导入这些名字」后"
                           "会先列出将要新增和移除的名单，你确认了才生效。\n"
                           "若导入的是整个部门/公司的大名单，请取消下面的勾选——"
                           "勾上会让这些人全部成为「我方成员」，「我方」认错了会把"
                           "客户的发言当成我们的答复。"
                      ).pack(anchor="w", padx=12)

            foot2 = ttk.Frame(dlg)
            foot2.pack(fill="x", padx=12, pady=(4, 10))

            var_use = tk.BooleanVar(value=True)
            ttk.Checkbutton(
                foot2, text="导入后同时勾选为本次使用",
                variable=var_use).pack(side="left")

            def brief(names: list) -> str:
                """名单太长就截断——弹窗要放得下，也不能一屏名字吓到人。"""
                head = "、".join(names[:12])
                return head if len(names) <= 12 else f"{head} 等共 {len(names)} 人"

            def replace_summary(filename: str, added: list, removed: list,
                                total: int) -> str:
                lines = [f"将按「{filename}」更新名称库：", ""]
                lines.append(f"· 新增 {len(added)} 人：{brief(added)}" if added
                             else "· 没有新增")
                lines.append(f"· 移除 {len(removed)} 人：{brief(removed)}")
                lines += ["",
                          "被移除的人如果现在勾着，会一并取消勾选——勾选的人在点"
                          "「确定」时会顺手入库，不取消就等于删不掉。",
                          "",
                          f"更新后名称库共 {total} 人。"]
                return "\n".join(lines)

            def do_add() -> None:
                """按导入的文件更新名称库：以文件为准，库里多出来的人会被移除。"""
                col = by_label(var_col.get())
                use = bool(var_use.get())
                incoming = list(col.names)                 # 以这个文件为准
                keep = set(incoming)
                removed = [n for n in library if n not in keep]
                added = [n for n in incoming if n not in library]
                total = len(library) - len(removed) + len(added)
                if not removed and not added:
                    dlg.destroy()
                    var_state.set(f"名称库与「{Path(path).name}」一致，没有变化")
                    return
                # 只有**会删人**时才拦一道：纯新增没有数据损失，不必多按一次。
                # 拦的理由——导错文件（比如翻出一份旧模版）会把库里的人清掉，
                # 而名称库是长期资产，误清一次就得重新导一遍。
                if removed and not messagebox.askokcancel(
                        "确认更新名称库",
                        replace_summary(Path(path).name, added, removed, total),
                        parent=dlg):
                    return
                # 移除必须**同时取消勾选**：点「确定」时"勾选的人顺手入库"
                # （见 ok() 里的 merge_names(library, names)），
                # 只从 library 删、state 里还勾着，那人会被原样加回库里。
                for n in removed:
                    state.pop(n, None)
                    rows.pop(n, None)
                library[:] = incoming        # 就地替换：外面几个闭包共用这个 list
                # 勾了才设为本次使用。导一份大花名册（整个部门/公司）时应当取消勾选：
                # 那批人会全部变成「我方成员」，而「我方」认错了会让**客户的发言
                # 被当成我们的答复**，问答清单内容错配，比漏掉更难发现。
                for n in incoming:
                    state[n] = use
                    rows.setdefault(n, Candidate(name=n, cid="（导入）",
                                                 reasons=[f"导入自 {Path(path).name}"]))
                var_lib.set(f"名称库：{len(library)} 人")
                dlg.destroy()
                draw()
                parts = []
                if added:
                    parts.append(f"新增 {len(added)} 人")
                if removed:
                    parts.append(f"移除 {len(removed)} 人")
                what = f"已按「{Path(path).name}」更新名称库（{'、'.join(parts)}）"
                var_state.set(
                    f"{what}，并勾选为本次使用" if use
                    else f"{what}（只入库，未勾选；要用请在上表勾上）")

            ttk.Button(foot2, text="取消", command=dlg.destroy).pack(side="right")
            ttk.Button(foot2, text="导入这些名字", command=do_add).pack(
                side="right", padx=6)

        def call_export_template() -> None:
            """导出模版：表头（必填标红）+ 把当前名称库的人带出去，改完再导入。"""
            p = filedialog.asksaveasfilename(
                title="保存 Excel 模版", parent=win,
                defaultextension=".xlsx",
                initialfile=name_library.TEMPLATE_FILENAME,
                filetypes=[("Excel 工作簿", "*.xlsx")])
            if not p:
                return
            try:
                out = name_library.write_template(p, library)
            except Exception as e:                                   # noqa: BLE001
                messagebox.showerror(
                    "导出失败",
                    f"写不了这个文件：{e}\n\n"
                    "常见原因：同名文件正被 Excel 打开着。关掉后重试即可。",
                    parent=win)
                return
            messagebox.showinfo(
                "模版已导出",
                f"已保存到：\n{out}\n\n"
                f"模版里已经带出当前名称库的 {len(library)} 个人"
                "（填在「姓名」列，红色表头那一列）。\n\n"
                "你可以直接在上面改：\n"
                "· 要加人：在最后一行下面接着写名字；\n"
                "· 要删人：把那一行整行删掉；\n"
                "· 部门 / 手机号 / 备注随便填，导入时会被忽略"
                "（名称库只存姓名）。\n\n"
                "改完回到这里，点「导入 Excel…」选这个文件——"
                "导入会按这个文件更新名称库，文件里没有的人会从库里移除，"
                "所以导入前会先列出将要新增和移除的名单，你确认了才生效。",
                parent=win)

        btn_import = ttk.Button(librow, text="导入 Excel…", width=12,
                                command=call_import)
        btn_import.pack(side="left", padx=(10, 0))
        ttk.Button(librow, text="导出 Excel 模版", width=15,
                   command=call_export_template).pack(side="left", padx=(6, 0))

        wrap = ttk.Frame(win)
        wrap.pack(fill="both", expand=True, padx=12)
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        tv = ttk.Treeview(wrap, show="headings", selectmode="browse",
                          columns=("chk", "name", "cid", "cnt", "why"))
        for cid, text, w, stretch in (
                ("chk", "选", 36, False), ("name", "名字", 150, False),
                ("cid", "账号", 190, False), ("cnt", "发言", 56, False),
                ("why", "来源 / 判断依据", 390, True)):
            tv.heading(cid, text=text)
            tv.column(cid, width=w, stretch=stretch, minwidth=36,
                      anchor="w" if cid in ("name", "cid", "why") else "center")
        tv.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=tv.yview)
        sb.grid(row=0, column=1, sticky="ns")
        # 横向滚动条：五列加起来比窗口最窄时宽（来源/判断依据那列尤其长），
        # 没有它的话窗口一小，最右边那列就看不全、也滚不过去。
        sb_x = ttk.Scrollbar(wrap, orient="horizontal", command=tv.xview)
        sb_x.grid(row=1, column=0, sticky="ew")
        tv.configure(yscrollcommand=sb.set, xscrollcommand=sb_x.set)

        var_sum = tk.StringVar(value="")
        lbl_sum = ttk.Label(win, textvariable=var_sum, style="Hint.TLabel",
                            wraplength=850, justify="left")

        def refresh_sum() -> None:
            names = [n for n, on in state.items() if on]
            lbl_sum.configure(
                text=(f"已选 {len(names)} 人：" + "、".join(names)) if names
                else "已选 0 人：不填的话，群聊里同事的答复会被当成客户发言。")

        def draw() -> None:
            keep = tv.selection()
            tv.delete(*tv.get_children())
            for i, name in enumerate(rows):
                c = rows[name]
                tv.insert("", "end", iid=str(i),
                          values=("☑" if state.get(name) else "☐", c.name, c.cid,
                                  c.msg_count or "", "；".join(c.reasons)))
            if keep and tv.exists(keep[0]):
                tv.selection_set(keep[0])
            refresh_sum()

        def toggle(_event=None):
            sel = tv.selection()
            if not sel:
                return
            name = list(rows)[int(sel[0])]
            state[name] = not state.get(name, False)
            tv.set(sel[0], "chk", "☑" if state[name] else "☐")
            refresh_sum()

        def add_manual(_event=None):
            """手动输入：同时勾上并存进名称库（用户主动输的，意图明确）。

            刻意**不过滤**用户敲进来的名字——静默丢掉会让人以为"加了没生效"。
            但会提示不像人名的输入：名称库是用部分匹配判我方身份的，
            一个泛词（部门名、手机号）留在库里可能误伤真实客户发言。
            """
            added = split_names(var_new.get())
            if not added:
                return
            for n in added:
                state[n] = True
                rows.setdefault(n, Candidate(name=n, cid="（手动添加）",
                                             reasons=["手动添加"]))
                if n not in library:
                    library.append(n)
            var_new.set("")
            var_lib.set(f"名称库：{len(library)} 人")
            odd = name_library.suspect_names(added)
            if odd:
                var_state.set(f"已添加，但「{'、'.join(odd)}」看起来不像人名，建议核对")
            draw()

        def remove_from_library(_event=None):
            """从名称库删除选中的人（取消勾选做不到这件事，必须显式删）。"""
            sel = tv.selection()
            if not sel:
                messagebox.showinfo("先选一行", "请先在表里点一下要删除的人。",
                                    parent=win)
                return
            name = list(rows)[int(sel[0])]
            if name not in library:
                messagebox.showinfo(
                    "不在名称库里",
                    f"「{name}」还不在名称库里（可能是扫描出来的候选），"
                    "不用删；不勾选它就不会被使用。", parent=win)
                return
            if not messagebox.askyesno(
                    "从名称库删除",
                    f"确定把「{name}」从名称库里删掉吗？\n\n"
                    "删掉后本次也不再使用它；以后需要可以重新添加或导入。",
                    parent=win):
                return
            library.remove(name)
            state.pop(name, None)
            rows.pop(name, None)
            var_lib.set(f"名称库：{len(library)} 人")
            draw()

        # 后台扫描：抓取聊天记录是重 IO，绝不能卡住界面
        scan_q: "queue.Queue[tuple]" = queue.Queue()
        # 轮询开关。poll 只是个"读队列→刷新界面"的循环，必须有人**启动**它；
        # 之前漏了启动，结果后台线程把结果塞进 scan_q 却没人读，
        # 界面永远停在"正在准备…"——所以这里用显式开关，扫完就停。
        polling = {"on": False}

        def scan_work():
            try:
                src = pipeline.build_source(self.cfg)
            except Exception as e:                                   # noqa: BLE001
                scan_q.put(("err", f"打开数据源失败：{e}"))
                return
            # 已在表里的人不再作为候选（它们已经在表里了），只找新的人
            known = list(state) + [n for n in library if n not in state]
            found: Dict[str, Candidate] = {}
            for i, c in enumerate(picked, start=1):
                scan_q.put(("prog", f"[{i}/{len(picked)}] 正在看「{c.name}」…"))
                try:
                    msgs = src.fetch(c, start, end)
                except Exception:                                    # noqa: BLE001
                    continue
                for cand in suggest_self_members(
                        msgs, known_self=known, owner_names=known + [c.name],
                        min_msgs=2, top=40):
                    prev = found.get(cand.name)
                    if prev is None or cand.score > prev.score:
                        found[cand.name] = cand
            scan_q.put(("done", sorted(found.values(),
                                       key=lambda x: (-x.score, -x.msg_count))))

        def do_scan():
            if not picked:
                messagebox.showinfo(
                    "先选聊天对象",
                    "请先在第 ① 步「加载聊天对象」，并在第 ② 步勾选至少一个群聊，"
                    "再来扫描。\n\n如果你已经知道同事的名字，也可以用「手动添加」"
                    "或「导入 Excel…」。", parent=win)
                return
            if start is None or end is None:
                messagebox.showerror("日期填写有误",
                                     "请先在第 ③ 步把开始日期和结束日期填好，再扫描。",
                                     parent=win)
                return
            btn_scan.configure(state="disabled")
            var_state.set("正在准备…")
            start_polling()                       # 不启动就没人读 scan_q，界面会一直卡住
            threading.Thread(target=scan_work, daemon=True).start()

        btn_scan.configure(command=do_scan)
        btn_add.configure(command=add_manual)
        ent_new.bind("<Return>", add_manual)

        def start_polling():
            """启动轮询循环（重复点扫描不会起两个循环）。"""
            if polling["on"]:
                return
            polling["on"] = True
            win.after(150, poll)

        def poll():
            try:
                while True:
                    kind, payload = scan_q.get_nowait()
                    if kind == "prog":
                        var_state.set(payload)
                    elif kind == "err":
                        var_state.set("扫描失败")
                        btn_scan.configure(state="normal")
                        polling["on"] = False          # 出错了，停掉循环
                        messagebox.showerror("扫描失败", payload, parent=win)
                    elif kind == "done":
                        found = list(payload)
                        self.self_candidates = found      # 记住，重开窗口不用再扫
                        for c in found:
                            rows[c.name] = c
                        var_state.set(f"找到 {len(found)} 个新候选" if found
                                      else "没有找到新的候选，可手动添加或导入 Excel")
                        btn_scan.configure(state="normal")
                        polling["on"] = False          # 扫完了，停掉循环
                        draw()
            except queue.Empty:
                pass
            try:
                if polling["on"] and win.winfo_exists():
                    win.after(150, poll)
            except tk.TclError:
                pass

        foot = ttk.Frame(win)
        foot.pack(fill="x", padx=12, pady=(6, 10))
        lbl_sum.pack(in_=foot, side="left")

        def ok():
            names = [n for n, on in state.items() if on]
            self.var_self_names.set(", ".join(names))
            # 入库口径是「本地库 + 本次勾选」：
            #   用本地库而不是 cfg 里的旧库，才不会把"导入了但这次没勾"的人丢掉；
            #   加上勾选的人，是因为勾选等于你认过他是同事，顺手存下来下次不用重输。
            #   取消勾选不入库删除——那只是"这次不用"。
            self.cfg["self_name_library"] = merge_names(library, names)
            self._sync_cfg()
            win.destroy()

        ttk.Button(foot, text="确定", command=ok).pack(side="right")
        ttk.Button(foot, text="取消", command=win.destroy).pack(side="right", padx=6)
        ttk.Button(foot, text="从名称库删除", command=remove_from_library).pack(
            side="right", padx=6)
        tv.bind("<Button-1>", toggle)
        tv.bind("<space>", toggle)

        # 复用上次扫到的候选，避免每次开窗都重扫一遍
        for c in getattr(self, "self_candidates", []) or []:
            rows.setdefault(c.name, c)
        draw()
        if not picked:
            var_state.set("请先勾选聊天对象；也可手动添加或导入 Excel")

    # ================= 设置与帮助 =================
    def _open_ai_settings(self) -> None:
        # 短总结场景选「关闭」可大幅降延迟与消耗。仅推理模型识别此参数，其它模型可留「自动」。
        _THINK_LABEL = {"": "自动", "disabled": "关闭（短总结推荐）", "enabled": "开启（深度思考）"}
        _THINK_VAL = {"自动": "", "关闭（短总结推荐）": "disabled", "开启（深度思考）": "enabled"}
        _cur_think = _THINK_LABEL.get(self.cfg.get("llm_thinking", ""), "自动")

        win = tk.Toplevel(self)
        win.title("AI 总结设置")
        win.geometry("600x540")
        win.transient(self)
        win.grab_set()
        frm = ttk.Frame(win, padding=14)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="填写任一 OpenAI 兼容接口即可（默认月之暗面 Kimi）。也可直接选本机 "
                            "WorkBuddy 已配置的模型，生成时即调用该模型。",
                  style="Hint.TLabel", wraplength=560
                  ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
        v_provider = tk.StringVar(value="WorkBuddy 模型" if self.cfg.get("llm_provider") == "workbuddy" else "手动填写")
        v_wb = tk.StringVar(value="")
        v_url = tk.StringVar(value=self.cfg.get("llm_base_url", ""))
        v_model = tk.StringVar(value=self.cfg.get("llm_model", ""))
        v_key = tk.StringVar(value=self.cfg.get("llm_api_key", ""))
        v_think = tk.StringVar(value=_cur_think)
        v_maxtok = tk.StringVar(value=str(self.cfg.get("llm_max_tokens", 2048)))
        v_temp = tk.StringVar(value=str(self.cfg.get("llm_temperature", 0.3)))

        # 行1：模型来源
        ttk.Label(frm, text="模型来源：").grid(row=1, column=0, sticky="w", pady=4)
        provider_combo = ttk.Combobox(frm, textvariable=v_provider, state="readonly",
                                     values=["手动填写", "WorkBuddy 模型"])
        provider_combo.grid(row=1, column=1, columnspan=2, sticky="ew")

        # 行2：WorkBuddy 模型（来源切到 WorkBuddy 时才显示）
        wb_label = ttk.Label(frm, text="WorkBuddy 模型：")
        wb_combo = ttk.Combobox(frm, textvariable=v_wb, state="readonly")
        wb_refresh = ttk.Button(frm, text="刷新", width=6)

        wb_models: List[Dict] = []
        wb_name_to_model: Dict[str, Dict] = {}

        def _load_wb():
            wb_models.clear()
            wb_name_to_model.clear()
            try:
                wb_models.extend(workbuddy_models.scan_workbuddy_models())
            except Exception:
                wb_models.clear()
            names = []
            for m in wb_models:
                label = workbuddy_models.display_label(m)
                wb_name_to_model[label] = m
                names.append(label)
            if not names:
                wb_combo.config(state="disabled")
                wb_combo.set("（未找到本机 WorkBuddy 模型配置）")
                return
            wb_combo.config(state="readonly")
            wb_combo["values"] = names
            cur = self.cfg.get("llm_wb_model", "")
            for label, m in wb_name_to_model.items():
                if m["id"] == cur:
                    wb_combo.set(label)
                    _on_wb()          # 预选后把 url/key/模型/思考 填进下方手动框
                    break
            else:
                if not v_wb.get():
                    wb_combo.set(names[0])

        def _on_wb(*_a):
            m = wb_name_to_model.get(v_wb.get())
            if not m:
                return
            r = workbuddy_models.resolve_for_engine(m)
            v_url.set(r["llm_base_url"])
            v_model.set(r["llm_model"])
            v_key.set(r["llm_api_key"])
            v_think.set(_THINK_LABEL.get(r["llm_thinking"], "自动"))
            v_temp.set(str(r.get("llm_temperature", 0.3)))

        def _on_provider(*_a):
            if v_provider.get() == "WorkBuddy 模型":
                wb_label.grid(row=2, column=0, sticky="w", pady=4)
                wb_combo.grid(row=2, column=1, sticky="ew")
                wb_refresh.grid(row=2, column=2, sticky="w", padx=(6, 0))
                _load_wb()
            else:
                wb_label.grid_remove()
                wb_combo.grid_remove()
                wb_refresh.grid_remove()

        provider_combo.bind("<<ComboboxSelected>>", _on_provider)
        wb_combo.bind("<<ComboboxSelected>>", _on_wb)
        wb_refresh.configure(command=lambda: _load_wb())

        # 行3 起：手动填写区（选 WorkBuddy 模型时会自动被填充，可改）
        rows = (
            ("接口地址 Base URL", v_url, ""),
            ("模型名称", v_model, ""),
            ("API Key", v_key, "*"),
            ("思考深度", v_think, "combo"),
            ("温度", v_temp, ""),
            ("最大输出 token", v_maxtok, ""),
        )
        for i, (label, var, kind) in enumerate(rows, start=3):
            ttk.Label(frm, text=label + "：").grid(row=i, column=0, sticky="w", pady=4)
            if kind == "combo":
                ttk.Combobox(frm, textvariable=var, state="readonly",
                             values=list(_THINK_VAL.keys())).grid(
                                 row=i, column=1, sticky="ew")
            else:
                ttk.Entry(frm, textvariable=var, show=kind).grid(
                    row=i, column=1, columnspan=2, sticky="ew")

        ttk.Label(frm, text="密钥保存在本机用户目录，不会随工具分发。也可用环境变量 "
                            "MOONSHOT_API_KEY / OPENAI_API_KEY 注入，优先级更高。",
                  style="Hint.TLabel", wraplength=560, justify="left"
                  ).grid(row=9, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Label(frm, text="「思考深度=关闭」适用于 hy3 等推理模型做 50-200 字短总结，避免思考占满预算导致正文为空；"
                            "但 kimi 系推理模型拒绝「关闭」、且温度只接受 1，故选 WorkBuddy 模型时已按模型自动设好温度与思考，一般无需手改。",
                  style="Hint.TLabel", wraplength=560, justify="left"
                  ).grid(row=10, column=0, columnspan=3, sticky="w", pady=(4, 0))

        row = ttk.Frame(frm)
        row.grid(row=11, column=0, columnspan=3, sticky="e", pady=(14, 0))

        def save():
            try:
                max_tok = int(v_maxtok.get().strip()) if v_maxtok.get().strip() else 2048
            except ValueError:
                max_tok = 2048
            try:
                temperature = float(v_temp.get().strip()) if v_temp.get().strip() else 0.3
            except ValueError:
                temperature = 0.3
            provider = "workbuddy" if v_provider.get() == "WorkBuddy 模型" else ""
            wb_id = ""
            if provider == "workbuddy":
                m = wb_name_to_model.get(v_wb.get())
                if not m:
                    messagebox.showerror("未选择 WorkBuddy 模型",
                                         "来源选了「WorkBuddy 模型」，请先在下拉里选一个具体模型"
                                         "（或改回「手动填写」）。")
                    return
                wb_id = m["id"]
            self.cfg.update(llm_base_url=v_url.get().strip(),
                            llm_model=v_model.get().strip(),
                            llm_api_key=v_key.get().strip(),
                            llm_thinking=_THINK_VAL.get(v_think.get(), ""),
                            llm_max_tokens=max_tok,
                            llm_temperature=temperature,
                            llm_provider=provider,
                            llm_wb_model=wb_id)
            config.save(self.cfg)
            win.destroy()
            self.var_status.set("AI 设置已保存。")

        # 左：腾讯云 DSV4 免费额度获取（跳转控制台领取，用默认浏览器打开）
        _DSV4_URL = ("https://console.cloud.tencent.com/tokenhub/models/detail"
                     "?modelId=deepseek-v4-flash-0731&regionId=1&from=all")

        def _open_dsv4_free_quota() -> None:
            try:
                webbrowser.open(_DSV4_URL)
            except Exception as exc:
                messagebox.showerror("打开失败",
                                     f"无法打开浏览器，请手动复制以下链接前往：\n{_DSV4_URL}\n\n{exc}")

        ttk.Button(row, text="腾讯云DSV4免费额度获取",
                   command=_open_dsv4_free_quota).pack(side="left")

        def _open_import_key() -> None:
            """打开窗口, 粘贴腾讯云 cURL, 解析后自动填入接口地址/模型/API Key。"""
            dlg = tk.Toplevel(win)
            dlg.title("导入 API Key（粘贴 cURL）")
            dlg.geometry("620x420")
            dlg.transient(win)
            dlg.grab_set()
            f = ttk.Frame(dlg, padding=12)
            f.pack(fill="both", expand=True)
            f.columnconfigure(0, weight=1)
            f.rowconfigure(1, weight=1)
            ttk.Label(f, text="把控制台里复制的 cURL 命令粘贴到下面，点「导入」自动填写 "
                               "接口地址 / 模型名称 / API Key：",
                      style="Hint.TLabel", wraplength=580, justify="left"
                      ).grid(row=0, column=0, sticky="w", pady=(0, 8))
            txt = tk.Text(f, wrap="word", height=12, font=("Consolas", 9))
            txt.grid(row=1, column=0, sticky="nsew")
            sb = ttk.Scrollbar(f, orient="vertical", command=txt.yview)
            sb.grid(row=1, column=1, sticky="ns")
            txt.configure(yscrollcommand=sb.set)
            btnf = ttk.Frame(f)
            btnf.grid(row=2, column=0, columnspan=2, sticky="e", pady=(10, 0))

            def do_import():
                raw = txt.get("1.0", "end").strip()
                if not raw:
                    messagebox.showwarning("请输入", "请先粘贴 cURL 命令内容。", parent=dlg)
                    return
                url, model, key = parse_curl_config(raw)
                if not (url or model or key):
                    messagebox.showerror("解析失败",
                                         "未能从内容里识别出接口地址/模型/API Key。\n"
                                         "请确认粘贴的是完整的 cURL 命令（含 URL、"
                                         "Authorization: Bearer 与 model 字段）。", parent=dlg)
                    return
                filled = []
                if url:
                    v_url.set(url); filled.append("接口地址")
                if model:
                    v_model.set(model); filled.append("模型名称")
                if key:
                    v_key.set(key); filled.append("API Key")
                dlg.destroy()
                self.var_status.set("已导入：" + "、".join(filled) + "。请确认后点「保存」。")

            ttk.Button(btnf, text="导入", command=do_import).pack(side="right")
            ttk.Button(btnf, text="取消", command=dlg.destroy).pack(side="right", padx=6)
            txt.focus_set()

        ttk.Button(row, text="导入API KEY", command=_open_import_key
                   ).pack(side="left", padx=(6, 0))
        # 右：保存 / 取消
        ttk.Button(row, text="保存", command=save).pack(side="right")
        ttk.Button(row, text="取消", command=win.destroy).pack(side="right", padx=6)

        # 初始化显隐（按当前 cfg 的来源决定第2行是否显示）
        _on_provider()

    def _open_cfg_dir(self) -> None:
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self._open_file(str(config.CONFIG_DIR))

    def _show_help(self) -> None:
        messagebox.showinfo("使用说明", (
            "六步走完，全程不用敲命令：\n\n"
            "① 选择聊天记录来源。默认「本机微信 4.x 加密库直读」，自动探测本机微信并读取。\n"
            "② 点「加载聊天对象」，在列表里勾选客户。可以先搜索再勾，搜索不会清空已勾选；"
            "对象很多时点「独立窗口选择…」更顺手。\n"
            "③ 选时间范围。点「近 7 天」「本月」这类按钮最省事，也可手填日期。\n"
            "④ 选总结方式 + 配 AI（可选）。默认「智能」，配了 AI 就用 AI，没配自动用本地总结；"
            "点「AI 总结设置…」可连 WorkBuddy 模型或任意 OpenAI 兼容接口。\n"
            "⑤ 设 Excel 保存位置（追加/覆盖）+ 客户阶段侧重（影响段落优先级）。\n"
            "⑥ 点「开始提取并生成总结」，下方表格双击可改文字，再「导出到 Excel」。\n\n"
            "Excel 固定三列：客户名称 / 时间范围 / 总结内容。\n"
            "提示：选择「本机微信 4.x 加密库直读」后，点击「抓取数据库密钥」即可一键获取密钥。"
        ))

    def _show_source_help(self) -> None:
        messagebox.showinfo("数据来源说明（重要）", (
            "微信的聊天记录存在本机一个加密数据库里，密钥在微信进程内存中。\n\n"
            "本工具提供四种取数方式：\n\n"
            "1) 本机微信 4.x 加密库直读（Windows）—— 自动探测本机已登录的微信 4.x 账号，"
            "只读扫描微信进程内存来抓取数据库密钥（无注入、无重启、无网络）。\n"
            "   · 首次使用请在微信保持登录状态，点「加载聊天对象」自动抓取密钥并缓存；\n"
            "   · 若密钥未缓存，可点「加载聊天对象」→ 自动触发抓取（需微信运行中）；\n"
            "   · 抓取失败时请**手动**退出微信（托盘图标右键 → 退出）再重新打开登录，然后重试；\n"
            "     本工具不会替你关闭微信。\n\n"
            "2) 内置样例数据 —— 免配置。用于试用、培训、验收产出格式。\n\n"
            "3) 导入聊天记录文件（推荐 · 跨平台）—— 把聊天记录导出成 csv / txt / json 或整包 .zip，"
            "本工具直接读（.zip 自动解压）。一个会话对应一个聊天对象，会话名取自导出里的真实名称。"
            "导出途径由用户自行掌握。\n\n"
            "4) 读取已解密的微信数据库 —— 选择已解密的 MicroMsg.db 与 MSG*.db（或整包 .zip 存档），"
            "本工具只读访问；解密由用户在自行信任的途径完成。\n\n"
            "【AI 总结】第④步「AI 总结设置…」里，「模型来源」选「WorkBuddy 模型」即可直接选用你本机 "
            "WorkBuddy 已配置的模型（kimi / deepseek / doubao 等），生成时即调用该模型，无需手填地址与密钥；"
            "也可选「手动填写」接任意 OpenAI 兼容接口。注意：WorkBuddy 的推理模型（kimi 系）不支持"
            "「思考深度=关闭」，保持「自动」即可。\n\n"
            "合规提醒：提取客户沟通记录前，请确认已获得公司授权，"
            "并遵守个人信息保护相关规定与客户约定。"
        ))

    def _show_about(self) -> None:
        messagebox.showinfo("关于", (
            f"{APP_TITLE}  v{__version__}（{_build_tag()}）\n\n"
            "面向客户成功团队的沟通记录归档工具。\n"
            "支持可视化操作、批量多选、AI（WorkBuddy / 任意 OpenAI 兼容）/ 本地双引擎总结、"
            "Excel 三列标准输出。\n"
            "同时提供命令行入口 cli.py，可接入 WorkBuddy 自动化工作流。\n\n"
            "改动源码后如需在打包版里生效，必须重新打包：\n"
            "1) pyinstaller wxcsm_app.spec --noconfirm --distpath dist\n"
            "2) python make_installer.py"
        ))

    def _on_close(self) -> None:
        if self.busy and not messagebox.askyesno("正在处理", "任务还没跑完，确定要退出吗？"):
            return
        try:
            self._sync_cfg()
        except Exception:
            pass
        self._stop_queue_poll()
        self.destroy()

    def _on_destroy(self, event=None) -> None:
        """<Destroy> 会为每个子控件冒泡触发，只认主窗口自己那一次。"""
        if event is not None and event.widget is not self:
            return
        self._stop_queue_poll()

    def _stop_queue_poll(self) -> None:
        """取消待触发的 _drain_queue 定时器。

        不取消的话，销毁窗口时那个已排队的 after 仍会触发，Tk 找不到已删除的
        命令就抛 `invalid command name "…_drain_queue"`——关闭程序时能在控制台
        看到。窗口都关了，这次轮询本来也没有意义。
        """
        aid = getattr(self, "_drain_id", None)
        if aid is not None:
            try:
                self.after_cancel(aid)
            except (tk.TclError, ValueError):
                pass
            self._drain_id = None


def _try_restore_existing() -> bool:
    """若已有实例的主窗口在运行(可能被最小化), 还原并置前, 返回 True。

    标题必须用 APP_TITLE_FULL（窗口标题带版本号）：和 self.title() 写进去的
    字符串不一致的话这里永远找不到窗口，单实例保护就形同虚设。
    """
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        hwnd = u.FindWindowW(None, APP_TITLE_FULL)
        if not hwnd:
            return False
        # SW_RESTORE=9; 最小化时还原到原位置并置前
        u.ShowWindow(hwnd, 9)
        u.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def main() -> None:
    # 单实例: 用一个命名互斥体判断是否已有实例在运行。
    # 场景: 用户把窗口最小化到任务栏后, 再次双击绿泡泡聊天记录总结.exe, 若已有实例,
    # 就直接把已有窗口还原置前并退出本次启动, 避免"打不开/多开多个"。
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        _mutex = kernel32.CreateMutexW(None, False, "Global\\wxcsm_绿泡泡聊天记录总结_single")
        already = kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
    except Exception:
        _mutex, already = None, False
    if already and _try_restore_existing():
        return  # 已有实例并已还原, 不重复启动

    app = App()
    app._mutex = _mutex  # 持有句柄, 防止被 GC 导致锁失效
    app.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # 窗口化启动时异常不会显示在控制台，必须自行记录日志
        import traceback as _tb
        from pathlib import Path as _P
        log_path = _P(__file__).resolve().parent / "绿泡泡聊天记录总结崩溃.log"
        try:
            log_path.write_text(_tb.format_exc(), encoding="utf-8")
        except Exception:
            pass
        try:
            import tkinter as _tk
            from tkinter import messagebox as _mb
            _root = _tk.Tk()
            _root.withdraw()
            _mb.showerror("启动失败",
                          f"工具启动出错：\n{e}\n\n"
                          f"详细错误已保存到：\n{log_path}\n\n"
                          f"请把该文件发给我，我来定位。")
            _root.destroy()
        except Exception:
            pass
        raise
