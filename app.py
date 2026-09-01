# -*- coding: utf-8 -*-
"""微信客户沟通记录提取与总结工具 —— 图形界面。

面向对象：完全没有编程与命令行经验的业务人员。
因此界面遵守三条铁律：
  1. 全流程单屏完成，不藏页签、不跳向导，步骤用 ①②③④ 明确编号；
  2. 任何报错都翻译成人话，并给出"下一步该干什么"；
  3. 关键操作可撤销、可预览、可人工修订后再导出。

一个刻意的设计：勾选状态与搜索框解耦。
  业务人员的真实操作是"搜张总→勾上→搜蔚蓝→再勾上→一起生成"。
  若沿用普通列表的多选，第二次搜索会清空第一次的勾选，用户会反复抓狂。
  所以这里用「勾选列 + 独立的已选集合」，搜索只影响显示，绝不影响已勾选的对象。
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import traceback
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import config, pipeline, workbuddy_models, wda_launcher
from core.models import Contact, Summary
from core.sources import SourceError, choices, REGISTRY

APP_TITLE = "微信客户沟通记录提取与总结工具"
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
RENDER_LIMIT = 2000          # 列表单次渲染上限：超过则只显示前 N 条并提示用搜索缩小（避免万级数据卡死）


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1180x830")
        self.minsize(1040, 720)

        self.cfg: Dict = config.load()
        self.contacts: List[Contact] = []
        self.filtered: List[Contact] = []   # 当前搜索+筛选后的全集（用于计数）
        self.visible: List[Contact] = []    # 实际渲染的行（受 RENDER_LIMIT 限制）
        self.picked: set[str] = set()
        self.summaries: List[Summary] = []
        self.msg_q: "queue.Queue[tuple]" = queue.Queue()
        self.busy = False

        self._source_meta = {c[0]: c for c in choices()}

        self._init_style()
        self._build_menu()
        self._build_ui()
        self._on_source_change()
        self._apply_preset("7d")
        self.after(120, self._drain_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

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

        self.var_source = tk.StringVar(value=self.cfg.get("source", "demo"))
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

        # 我方标识名：导入源（真实姓名场景无「我方」标记）与解密数据库源（群聊 is_sender 偶尔归错人）都用它
        self.frm_self = ttk.Frame(f1)
        self.frm_self.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self.frm_self.columnconfigure(1, weight=1)
        ttk.Label(self.frm_self, text="我方标识名：").grid(row=0, column=0, sticky="w")
        self.var_self_names = tk.StringVar(value=", ".join(self.cfg.get("self_names") or []))
        ttk.Entry(self.frm_self, textvariable=self.var_self_names).grid(
            row=0, column=1, sticky="ew", padx=4)
        ttk.Label(self.frm_self, text="（多个逗号分隔；数据库源用它校正群聊归错人，并统一显示你的名字）",
                  style="Hint.TLabel").grid(row=0, column=2)

        bar = ttk.Frame(f1)
        bar.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Button(bar, text="加载聊天对象", command=self._load_contacts).pack(side="left")
        self.lbl_src_state = ttk.Label(bar, text="尚未加载", style="Hint.TLabel")
        self.lbl_src_state.pack(side="left", padx=8)

        # 辅助工具：WeChatDataAnalysis 启动器（已装→直接启动；未装→打开安装包）
        wda = ttk.Frame(f1)
        wda.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.btn_wda = ttk.Button(wda, text="启动 WeChatDataAnalysis",
                                  command=self._launch_wda)
        self.btn_wda.pack(side="left")
        self.lbl_wda = ttk.Label(wda, text="", style="Hint.TLabel")
        self.lbl_wda.pack(side="left", padx=8)
        self._refresh_wda_status()

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
            self.tv.heading(cid, text=text)
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
        ttk.Label(f3, text="开始日期：").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(f3, textvariable=self.var_start, width=14).grid(row=1, column=1, sticky="w")
        ttk.Label(f3, text="结束日期：").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Entry(f3, textvariable=self.var_end, width=14).grid(row=2, column=1, sticky="w")
        ttk.Label(f3, text="格式 2026-08-01，含首尾两天。所选每个对象各生成一篇总结。",
                  style="Hint.TLabel", wraplength=330, justify="left"
                  ).grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # ④ 总结方式
        f4 = ttk.Labelframe(right, text=" ④ 选择总结生成方式 ", padding=10)
        f4.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        f4.columnconfigure(1, weight=1)
        self.var_engine = tk.StringVar(value=self.cfg.get("engine", "auto"))
        for i, (val, text) in enumerate((
            ("auto", "智能（优先 AI，不可用时自动改用本地总结）"),
            ("ai", "仅用 AI 生成（质量最好，需联网与密钥）"),
            ("offline", "仅用本地生成（不联网、不外发数据）"),
        )):
            ttk.Radiobutton(f4, text=text, value=val, variable=self.var_engine
                            ).grid(row=i, column=0, columnspan=3, sticky="w", pady=1)
        crow = ttk.Frame(f4)
        crow.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Label(crow, text="总结字数：").pack(side="left")
        self.var_min = tk.StringVar(value=str(self.cfg.get("min_chars", 50)))
        self.var_max = tk.StringVar(value=str(self.cfg.get("max_chars", 200)))
        ttk.Entry(crow, textvariable=self.var_min, width=5).pack(side="left")
        ttk.Label(crow, text=" ~ ").pack(side="left")
        ttk.Entry(crow, textvariable=self.var_max, width=5).pack(side="left")
        ttk.Label(crow, text=" 字").pack(side="left")
        ttk.Button(crow, text="AI 设置…", command=self._open_ai_settings).pack(side="right")

        # 客户阶段（影响总结侧重与段落优先级；留空=按对话关键词自动推断）
        srow_stage = ttk.Frame(f4)
        srow_stage.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Label(srow_stage, text="客户阶段：").pack(side="left")
        self.var_stage = tk.StringVar(value=self.cfg.get("customer_stage") or "自动推断")
        self.cbo_stage = ttk.Combobox(
            srow_stage, state="readonly", width=14,
            textvariable=self.var_stage,
            values=["自动推断", "续费期", "实施中", "新签客户", "稳定使用"])
        self.cbo_stage.pack(side="left", padx=(4, 0))
        ttk.Label(srow_stage, text="（续费期优先保留风险，稳定使用偏进展）",
                  style="Hint.TLabel").pack(side="left", padx=6)

        # ⑤ 导出
        f5 = ttk.Labelframe(right, text=" ⑤ 设置 Excel 保存位置 ", padding=10)
        f5.grid(row=2, column=0, sticky="ew", pady=(8, 0))
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
        ttk.Label(f5, text="固定三列：客户名称 / 时间范围 / 总结内容。",
                  style="Hint.TLabel").grid(row=2, column=0, columnspan=2,
                                            sticky="w", pady=(6, 0))

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
    def _build_result(self, root) -> None:
        f = ttk.Labelframe(root, text=" ⑥ 总结结果（双击任意一行可人工修订后再导出） ", padding=10)
        f.grid(row=2, column=0, columnspan=2, sticky="nsew")
        f.columnconfigure(0, weight=1)
        f.rowconfigure(0, weight=1)

        wrap = ttk.Frame(f)
        wrap.grid(row=0, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        cols = ("name", "range", "content", "meta")
        self.tv_res = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="browse")
        for cid, text, w, stretch in (
            ("name", "客户名称", 190, False), ("range", "时间范围", 170, False),
            ("content", "总结内容", 640, True), ("meta", "字数/方式", 110, False),
        ):
            self.tv_res.heading(cid, text=text)
            self.tv_res.column(cid, width=w, stretch=stretch, anchor="w")
        self.tv_res.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tv_res.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.tv_res.configure(yscrollcommand=sb.set)
        self.tv_res.bind("<Double-1>", self._edit_summary)

        brow = ttk.Frame(f)
        brow.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.btn_export = ttk.Button(brow, text="导出到 Excel", command=self._export)
        self.btn_export.pack(side="left")
        ttk.Button(brow, text="打开 Excel 文件", command=self._open_export).pack(side="left", padx=6)
        ttk.Button(brow, text="修订选中总结", command=self._edit_summary).pack(side="left")
        ttk.Button(brow, text="清空结果", command=self._clear_res).pack(side="left", padx=6)
        self.lbl_res = ttk.Label(brow, text="暂无结果", style="Hint.TLabel")
        self.lbl_res.pack(side="right")

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
        if key in ("import", "wxdb"):
            self.frm_self.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        else:
            self.frm_self.grid_remove()
        self.lbl_src_state.configure(text="尚未加载")

    # ---------- WeChatDataAnalysis 启动器 ----------
    def _refresh_wda_status(self) -> None:
        """根据探测结果刷新按钮文案与状态标签。"""
        try:
            st, _path = wda_launcher.detect_status()
        except Exception:
            st, _path = "missing", None
        if st == "installed":
            self.btn_wda.configure(text="启动 WeChatDataAnalysis")
            self.lbl_wda.configure(text="已安装 ✓", foreground=CLR_OK)
        elif st == "not_installed":
            self.btn_wda.configure(text="安装并启动 WeChatDataAnalysis")
            self.lbl_wda.configure(text="未安装 · 点击打开安装包", foreground=CLR_MUTE)
        else:
            self.btn_wda.configure(text="安装 WeChatDataAnalysis")
            self.lbl_wda.configure(text="未安装 · 缺安装包", foreground=CLR_ERR)

    def _launch_wda(self) -> None:
        """点击 WeChatDataAnalysis 按钮：已装则启动，未装则打开安装包。"""
        try:
            res = wda_launcher.launch_wda()
        except Exception as e:  # 拉起失败不要静默
            self.var_status.set(f"WeChatDataAnalysis 启动出错：{e}")
            return
        self._refresh_wda_status()
        if res["action"] == "launched":
            self.var_status.set("已启动 WeChatDataAnalysis。")
        elif res["action"] == "installer":
            messagebox.showinfo(
                "未安装，已打开安装包",
                "本机尚未检测到 WeChatDataAnalysis。\n\n"
                "已为你打开安装包，请按向导完成安装；安装完成后再次点击"
                "「启动 WeChatDataAnalysis」即可运行。")
            self.var_status.set("未安装，已打开安装包，请安装后重试。")
        else:
            pkg = wda_launcher.tools_root()
            messagebox.showerror(
                "未找到安装包",
                f"本机未安装 WeChatDataAnalysis，且 tools/wechatdataanalysis/ 下没有安装包。\n\n"
                f"请把{wda_launcher.installer_hint()}放到：\n"
                f"{pkg}/wechatdataanalysis/")
            self.var_status.set("未找到安装包，请先放入 tools/wechatdataanalysis/。")

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
        self.lbl_src_state.configure(text="正在加载…")
        self._set_busy(True)

        def work():
            try:
                items = pipeline.load_contacts(self.cfg)
                self.msg_q.put(("contacts", items))
            except SourceError as e:
                self.msg_q.put(("error", ("加载聊天对象失败", str(e))))
            except Exception as e:
                self.msg_q.put(("error", ("加载聊天对象出错", f"{e}\n\n{traceback.format_exc(limit=2)}")))
            finally:
                self.msg_q.put(("done_load", None))

        threading.Thread(target=work, daemon=True).start()

    # ================= 列表渲染 =================
    def _refresh_list(self) -> None:
        kw = self.var_search.get().strip()
        filtered = pipeline.filter_contacts(self.contacts, kw)
        if self.var_only_picked.get():
            filtered = [c for c in filtered if c.cid in self.picked]
        self.filtered = filtered
        shown = filtered[:RENDER_LIMIT]
        self.visible = shown
        self.tv.delete(*self.tv.get_children())
        for c in shown:
            on = c.cid in self.picked
            self.tv.insert(
                "", "end", iid=c.cid,
                values=(MARK_ON if on else MARK_OFF, c.name,
                        {"friend": "好友", "group": "群聊"}.get(c.kind, "—"),
                        c.msg_count or "—",
                        c.last_time.strftime("%Y-%m-%d") if c.last_time else "—"),
                tags=("on",) if on else (),
            )
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
        ttk.Label(srow, text="勾选实时生效，关闭窗口即应用。", style="Hint.TLabel"
                  ).grid(row=0, column=3, padx=10)

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
            p_tv.heading(cid, text=text)
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

    def _refresh_picker(self, win, p_search, p_only, p_tv, p_lbl) -> None:
        kw = p_search.get().strip()
        filtered = pipeline.filter_contacts(self.contacts, kw)
        if p_only.get():
            filtered = [c for c in filtered if c.cid in self.picked]
        shown = filtered[:RENDER_LIMIT]
        p_tv.delete(*p_tv.get_children())
        for c in shown:
            on = c.cid in self.picked
            p_tv.insert("", "end", iid=c.cid,
                        values=(MARK_ON if on else MARK_OFF, c.name,
                                {"friend": "好友", "group": "群聊"}.get(c.kind, "—"),
                                c.msg_count or "—",
                                c.last_time.strftime("%Y-%m-%d") if c.last_time else "—"),
                        tags=("on",) if on else ())
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
                    self.var_status.set(f"{title}：{msg.splitlines()[0]}")
                    messagebox.showerror(title, msg)
                elif kind in ("done_load", "done_run"):
                    self._set_busy(False)
                    if kind == "done_load":
                        self.lbl_src_state.configure(
                            text=self.lbl_src_state.cget("text").replace("正在加载…", "加载结束"))
        except queue.Empty:
            pass
        self.after(120, self._drain_queue)

    def _on_result(self, res: pipeline.RunResult) -> None:
        self.summaries.extend(res.summaries)
        self._render_results()
        self.pb.configure(value=self.pb.cget("maximum"))
        parts = [f"完成：生成 {res.ok_count} 篇总结"]
        if res.export_path:
            parts.append(f"已写入 {res.written} 行 → {res.export_path}")
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
        if res.ok_count:
            detail += "\n\n可在下方表格双击任意一行修订文字，改完点「导出到 Excel」再存一次。"
            messagebox.showinfo("处理完成", detail)
        else:
            messagebox.showwarning("没有生成任何总结", detail or "所选时间范围内没有找到聊天记录。")

    def _render_results(self) -> None:
        self.tv_res.delete(*self.tv_res.get_children())
        for i, s in enumerate(self.summaries):
            tag = "ai" if s.engine == "ai" else "off"
            self.tv_res.insert("", "end", iid=str(i),
                               values=(s.customer_name, s.time_range, s.content,
                                       f"{s.char_len} 字 / {'AI' if tag == 'ai' else '本地'}"))
        self.lbl_res.configure(text=f"共 {len(self.summaries)} 篇总结")

    # ================= 结果操作 =================
    def _current_index(self) -> Optional[int]:
        sel = self.tv_res.selection()
        if not sel:
            return None
        try:
            return int(sel[0])
        except ValueError:
            return None

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

    def _export(self) -> None:
        if not self.summaries:
            messagebox.showinfo("没有可导出的内容", "请先生成总结，再执行导出。")
            return
        self._sync_cfg()
        try:
            res = pipeline.export_only(self.cfg, self.summaries)
        except PermissionError as e:
            messagebox.showerror("文件被占用", str(e))
            return
        except Exception as e:
            messagebox.showerror("导出失败", str(e))
            return
        self.var_status.set(f"已导出 {res.written} 行 → {res.export_path}")
        if messagebox.askyesno("导出完成",
                               f"已写入 {res.written} 行，文件共 {res.total_rows} 行数据。\n\n"
                               f"{res.export_path}\n\n现在打开这个文件吗？"):
            self._open_file(res.export_path)

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
        if self.summaries and not messagebox.askyesno(
                "确认清空", "只清空界面上的结果列表，已导出的 Excel 文件不受影响。继续吗？"):
            return
        self.summaries.clear()
        self._render_results()

    def _pick_export(self) -> None:
        p = filedialog.asksaveasfilename(
            title="选择 Excel 保存位置", defaultextension=".xlsx",
            filetypes=[("Excel 工作簿", "*.xlsx")],
            initialfile=Path(self.var_export.get() or "微信客户沟通总结.xlsx").name)
        if p:
            self.var_export.set(p)

    # ================= 设置与帮助 =================
    def _open_ai_settings(self) -> None:
        # 思考深度：推理类模型(hy3/kimi 等)默认会深度思考，把 max_tokens 预算吃光导致正文为空；
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
            "① 选择聊天记录来源。第一次用建议先选「内置样例数据」，跑一遍看产出格式。\n"
            "② 点「加载聊天对象」，在列表里勾选客户。可以先搜索再勾，搜索不会清空已勾选；"
            "对象很多时点「独立窗口选择…」更顺手。\n"
            "③ 选时间范围。点「近 7 天」「本月」这类按钮最省事，也可手填日期。\n"
            "④ 选总结方式 + 配 AI（可选）。默认「智能」，配了 AI 就用 AI，没配自动用本地总结；"
            "点「AI 总结设置…」可连 WorkBuddy 模型或任意 OpenAI 兼容接口。\n"
            "⑤ 设 Excel 保存位置（追加/覆盖）+ 客户阶段侧重（影响段落优先级）。\n"
            "⑥ 点「开始提取并生成总结」，下方表格双击可改文字，再「导出到 Excel」。\n\n"
            "Excel 固定三列：客户名称 / 时间范围 / 总结内容。\n"
            "提示：① 框底部有「启动 WeChatDataAnalysis」按钮，可一键拉起或安装该取数工具。"
        ))

    def _show_source_help(self) -> None:
        messagebox.showinfo("数据来源说明（重要）", (
            "微信的聊天记录存在本机一个加密数据库里，密钥在微信进程内存中。\n"
            "本工具刻意不去读微信进程内存，原因有三：这类手段会随微信版本更新失效、"
            "可能触发账号风控、在企业合规审查里也难以说清。\n\n"
            "因此提供三种取数方式：\n\n"
            "1) 内置样例数据 —— 免配置。用于试用、培训、验收产出格式。\n\n"
            "2) 导入聊天记录文件（推荐）—— 用 WeChatDataAnalysis / 留痕 / WeChatMsg 等工具把记录导出成 "
            "csv / txt / json 或整包 .zip，本工具直接读（.zip 自动解压）。一个会话对应一个聊天对象，"
            "会话名取自导出里的真实名称。这条路最稳、最好解释。\n"
            "   · 需要先导出微信数据？点 ① 框底部的「启动 WeChatDataAnalysis」：已装直接拉起，"
            "没装则打开安装包（安装包放 tools/wechatdataanalysis/ 下）。\n\n"
            "3) 读取已解密的微信数据库 —— 用外部工具解密出 MicroMsg.db 与 MSG*.db（或整包 .zip 存档），"
            "选那个压缩包或文件夹即可。本工具只以只读方式读取，不解密、不碰微信进程。\n\n"
            "【AI 总结】第④步「AI 总结设置…」里，「模型来源」选「WorkBuddy 模型」即可直接选用你本机 "
            "WorkBuddy 已配置的模型（kimi / deepseek / doubao 等），生成时即调用该模型，无需手填地址与密钥；"
            "也可选「手动填写」接任意 OpenAI 兼容接口。注意：WorkBuddy 的推理模型（kimi 系）不支持"
            "「思考深度=关闭」，保持「自动」即可。\n\n"
            "合规提醒：提取客户沟通记录前，请确认已获得公司授权，"
            "并遵守个人信息保护相关规定与客户约定。"
        ))

    def _show_about(self) -> None:
        from core import __version__
        messagebox.showinfo("关于", (
            f"{APP_TITLE}  v{__version__}\n\n"
            "面向客户成功团队的沟通记录归档工具。\n"
            "支持可视化操作、批量多选、AI（WorkBuddy / 任意 OpenAI 兼容）/ 本地双引擎总结、"
            "Excel 三列标准输出。\n"
            "同时提供命令行入口 cli.py，可接入 WorkBuddy 自动化工作流。"
        ))

    def _on_close(self) -> None:
        if self.busy and not messagebox.askyesno("正在处理", "任务还没跑完，确定要退出吗？"):
            return
        try:
            self._sync_cfg()
        except Exception:
            pass
        self.destroy()


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # 窗口化启动时异常不会显示在控制台，必须自己留痕
        import traceback as _tb
        from pathlib import Path as _P
        log_path = _P(__file__).resolve().parent / "启动工具崩溃.log"
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
