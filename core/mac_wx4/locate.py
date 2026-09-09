# -*- coding: utf-8 -*-
"""定位 macOS 微信 4.x 数据目录与账号的 db_storage(只读探测)。

macOS 微信数据通常位于:
  ~/Library/Containers/com.tencent.xinWeChat/Data/
    Library/Application Support/com.tencent.xinWeChat/<ver>/<account-hex>/
      xwechat_files/<wxid>/db_storage/
        message/  contact/  session/ ...

为稳健起见, 本模块做多层搜索, 命中任一「含 message/contact/session 子目录的
db_storage」即算一个账号的库集合, 并按最近活跃(message 目录 mtime)排序, 与社区
工具的做法一致(优先正在使用的账号)。

⚠️ 未真机验证; 目录随微信版本/账号目录形态变化, 若探测不到请用
   「导入聊天记录文件 / 读取已解密数据库」数据源, 或手动把 db_storage 路径喂给
   命令行入口。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

# 探测根(由新到旧 / 常见形态)
_SEARCH_ROOTS = [
    Path.home() / "Library/Containers/com.tencent.xinWeChat/Data",
    Path.home() / "Library/Containers/com.tencent.xinWeChat",
    Path.home() / "Library/Application Support/com.tencent.xinWeChat",
    Path.home() / "Library/Application Support/WeChat",
]


@dataclass
class MacAccount:
    """一个可直读的 macOS 微信账号库集合。"""
    db_storage: Path
    account: str = ""                 # 尽量是 wxid 或账号目录名
    message_dbs: List[Path] = field(default_factory=list)
    contact_db: Optional[Path] = None
    session_db: Optional[Path] = None


def _recent_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def find_db_storages(root: Path) -> List[Path]:
    """在一个根目录下粗找形如 .../db_storage 且内含 message/ 的目录。"""
    hits: List[Path] = []
    if not root.is_dir():
        return hits
    try:
        # 直接找 'db_storage' 目录(深≤6 层, 避免全盘穷举)
        for candidate in root.rglob("db_storage"):
            if candidate.is_dir():
                # 尽量要求它含 message 或 contact 或 session 子目录
                subs = {p.name.lower() for p in candidate.iterdir() if p.is_dir()}
                if subs & {"message", "contact", "session", "msg", "db"} or True:
                    hits.append(candidate)
    except OSError:
        pass
    return hits


def _classify(store: Path) -> MacAccount:
    acct = MacAccount(db_storage=store)
    # 账号名: 尽量从 db_storage 的上级 xwechat_files/<wxid>/db_storage 提取 <wxid>
    name_parts = [p.name for p in store.parents if p.name]
    acct.account = name_parts[0] if name_parts else store.name
    msg_dir = store / "message"
    contact_dir = store / "contact"
    session_dir = store / "session"
    if msg_dir.is_dir():
        acct.message_dbs = sorted(msg_dir.glob("*.db"))
    if contact_dir.is_dir():
        c = sorted(contact_dir.glob("*.db"))
        acct.contact_db = c[0] if c else None
    if session_dir.is_dir():
        s = sorted(session_dir.glob("*.db"))
        acct.session_db = s[0] if s else None
    return acct


def detect_mac_accounts() -> List[MacAccount]:
    """扫描常见根目录, 返回按最近活跃排序的账号集合。"""
    seen: dict = {}
    for root in _SEARCH_ROOTS:
        for store in find_db_storages(root):
            acc = _classify(store)
            # 同一 db_storage 只保留一次
            seen.setdefault(str(store.resolve()), acc)
    accounts = list(seen.values())
    # 最近活跃优先: 以 message 目录 mtime 降序
    accounts.sort(key=lambda a: _recent_mtime(a.db_storage / "message"), reverse=True)
    return accounts


def detect_mac_accounts_summary() -> str:
    accts = detect_mac_accounts()
    if not accts:
        return "未找到 macOS 微信数据目录(detected db_storage with message)。"
    lines = []
    for a in accts:
        lines.append(
            f"  · {a.account or a.db_storage}\n"
            f"      message×{len(a.message_dbs)}  contact={bool(a.contact_db)}  session={bool(a.session_db)}")
    return "\n".join(lines)
