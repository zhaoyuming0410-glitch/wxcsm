# -*- coding: utf-8 -*-
"""macOS 微信数据定位/读取 —— 尽力而为 + 需真机验证。

⚠️ 重要状态声明:
  本模块仅基于公开资料编写, 未在真实 macOS 微信上验证。macOS 微信的数据路径、库结构
  与加密策略会随版本变化; 现代版本(尤其 3.7+/4.x)往往对数据库做混淆/加密, 无法像
  Windows 4.x 那样可靠解出密钥来解密。因此本模块**不会**注册为 GUI 可用数据源, 以免
  误导用户以为它能直读当前 macOS 微信。

它能做什么(尽力):
  1. 定位 macOS 微信数据目录(公开路径), 报告是否存在。
  2. 若发现**历史明文库**(老版本 macOS 微信常把聊天库以明文 SQLite 存放), 可只读列出
     库文件并尝试读取联系人/消息表。
  3. 若发现加密/未知结构, 给出明确"不支持/需真机进一步逆向"的提示, 而非假装可用。

用法(仅命令行探测, 不接 GUI):
  python3 -m core.sources.mac_wx   # 打印探测结果

真实可用的取数途径: 若你已在 Mac 上把微信记录导出成 .zip / .db(用你自己信任的导出工具),
用「导入聊天记录文件」或「读取已解密数据库」数据源即可(这两者跨平台, macOS 上可用)。
"""

from __future__ import annotations

import os
import sys
import sqlite3
from pathlib import Path

# macOS 微信数据可能所在目录(公开资料汇总, 由新到旧排列)
SEARCH_ROOTS = [
    Path.home() / "Library" / "Containers" / "com.tencent.xinWeChat" / "Data",
    Path.home() / "Library" / "Containers" / "com.tencent.xinWeChat",
    Path.home() / "Library" / "Application Support" / "com.tencent.xinWeChat",
    Path.home() / "Library" / "Application Support" / "WeChat",
]
# 各账号数据容器目录常见子路径(账号为一串 hex)
ACCT_MARKERS = ("Message", "msg", "db_storage", "xwechat_files")


def _find_wechat_roots() -> list:
    found = []
    for root in SEARCH_ROOTS:
        if root.is_dir():
            found.append(root)
    return found


def _find_message_dirs(root: Path) -> list:
    """在给定根下粗扫与账号/消息库相关的子目录。"""
    hits = []
    try:
        for sub in root.iterdir():
            if not sub.is_dir():
                continue
            # 账号容器层: 子目录下常含 Message/msg 之类
            try:
                for s2 in sub.iterdir():
                    if s2.is_dir() and any(m in s2.name for m in ACCT_MARKERS):
                        hits.append(s2)
            except OSError:
                pass
            if any(m in sub.name for m in ACCT_MARKERS):
                hits.append(sub)
    except OSError:
        pass
    return hits


def _try_open_db(path: Path):
    """尝试只读打开一个 sqlite 库; 返回 (可读?, 是否明文?, 表数量)。"""
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 5")
        tables = [r[0] for r in cur.fetchall()]
        con.close()
        if tables:
            return True, True, len(tables)
        return True, True, 0
    except sqlite3.DatabaseError as e:
        # 打不开 -> 大概率加密或非 sqlite
        return False, False, 0
    except Exception:
        return False, False, 0


def scan() -> dict:
    """返回探测摘要。"""
    roots = _find_wechat_roots()
    out = {"data_dirs_found": [str(p) for p in roots],
           "message_dirs": [], "readable_plain_db": [], "encrypted_or_unknown": []}
    if not roots:
        return out
    for r in roots:
        for md in _find_message_dirs(r):
            out["message_dirs"].append(str(md))
            for db in sorted(md.rglob("*.db"))[:20]:
                ok, plain, nt = _try_open_db(db)
                if ok and plain:
                    out["readable_plain_db"].append(str(db))
                else:
                    out["encrypted_or_unknown"].append(str(db))
    # 去重
    for k in ("readable_plain_db", "encrypted_or_unknown", "message_dirs"):
        out[k] = list(dict.fromkeys(out[k]))
    return out


def main() -> int:
    print("macOS 微信数据探测 (仅尽力, 未真机验证):\n")
    r = scan()
    if not r["data_dirs_found"]:
        print("  未找到 macOS 微信数据目录。\n  (若已登录微信, 请确认目录名/版本; 或改用「导入文件」数据源)")
        return 0
    print("  数据目录:", "、".join(r["data_dirs_found"]))
    print("  消息相关子目录:", len(r["message_dirs"]))
    if r["readable_plain_db"]:
        print("\n  ✅ 可明文读取的库(老版本历史明文):")
        for p in r["readable_plain_db"]:
            print("     -", p)
    if r["encrypted_or_unknown"]:
        print("\n  ⚠️ 加密/未知结构库(当前版本无法直读):")
        for p in r["encrypted_or_unknown"][:10]:
            print("     -", p)
        print("  (现代 macOS 微信数据库常加密, 需真机逆向/导出; 建议用「导入/已解密库」数据源)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
