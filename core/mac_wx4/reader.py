# -*- coding: utf-8 -*-
"""扫描解密后的 macOS 微信明文库, 得到联系人/消息(尽力而为, 需真机校准)。

macOS 4.x 解密后结构与 Windows 4.x 接近(社区一致):
  message/message_*.db  → Msg_<md5(wxid)> 消息表(local_type/create_time/...)
  contact/contact.db    → contact 表(username,nick_name,remark,alias)
  session/session.db    → Name2Id(user_name = 本人账号)
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import date, datetime, time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..models import Contact, Message

TYPE_MAP = {
    1: "text", 3: "图片", 34: "语音", 43: "视频", 47: "表情",
    42: "名片", 48: "位置", 49: "文件/链接", 10000: "系统消息", 10002: "系统消息",
}
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
try:
    import zstandard as zstd
    _ZSTD = zstd.ZstdDecompressor()
    _HAVE_ZSTD = True
except Exception:
    _HAVE_ZSTD = False

_SELF_ID_STRIP = re.compile(r"[_\-][^_\-]+$")


def _is_decrypted(path: Path) -> bool:
    try:
        return path.read_bytes()[:16] == b"SQLite format 3\x00"
    except Exception:
        return False


def scan_contacts(root: Path) -> List[Contact]:
    """从 contact 类表读联系人; 返回排序列表。"""
    contacts: Dict[str, Contact] = {}
    for db in sorted(root.rglob("*.db")):
        if not _is_decrypted(db):
            continue
        try:
            con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=10)
        except sqlite3.Error:
            continue
        try:
            tables = [r[0].lower() for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
        except sqlite3.Error:
            con.close()
            continue
        if "contact" not in tables and "wccontact" not in tables:
            con.close()
            continue
        tb = "contact" if "contact" in tables else "wccontact"
        try:
            cols = [r[1].lower() for r in con.execute(f'PRAGMA table_info("{tb}")')]
        except sqlite3.Error:
            con.close()
            continue
        low = {c: c for c in cols}
        u = low.get("username") or low.get("user_name") or low.get("usr_name")
        nick = low.get("nickname") or low.get("nick_name")
        remark = low.get("remark")
        if not u:
            con.close()
            continue
        try:
            rows = con.execute(
                f'SELECT "{u}"' + (f',"{nick}"' if nick else "") +
                (f',"{remark}"' if remark else "") + f' FROM "{tb}"').fetchall()
        except sqlite3.Error:
            con.close()
            continue
        for row in rows:
            uid = (row[0] or "").strip()
            if not uid or uid.startswith(("gh_", "wxid_biz")):
                continue
            nk = (row[1] or "").strip() if nick and len(row) > 1 else ""
            rk = (row[2] or "").strip() if remark and len(row) > 2 else ""
            contacts.setdefault(uid, Contact(
                cid=uid,
                name=rk or nk or uid,
                kind="group" if uid.endswith("@chatroom") else "friend",
                alias=nk or ""))
        con.close()
    return sorted(contacts.values(), key=lambda c: c.name)


def _decode(cc, mc, lt):
    raw = None
    zstd_src = None
    for field in (mc, cc):
        if isinstance(field, bytes) and len(field) >= 4 and field[:4] == ZSTD_MAGIC:
            zstd_src = field
            break
    if zstd_src is not None:
        if _HAVE_ZSTD:
            try:
                raw = _ZSTD.decompress(zstd_src)
            except Exception:
                raw = None
        else:
            return "[ZSTD压缩 需安装zstandard库]"
    if raw is None:
        raw = (mc or b"") if isinstance(mc, bytes) else str(mc or "").encode("utf-8")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    text = raw.strip()
    if lt == 1:
        return text
    if text.startswith("<"):
        m = re.search(r"<title>(.*?)</title>", text, re.S)
        if m:
            return f"[{TYPE_MAP.get(lt, f'类型{lt}')}] {m.group(1).strip()}"
        plain = re.sub(r"<[^>]+>", "", text).strip()
        if plain:
            return f"[{TYPE_MAP.get(lt, f'类型{lt}')}] {plain}"
        return f"[{TYPE_MAP.get(lt, f'类型{lt}')}]"
    return f"[{TYPE_MAP.get(lt, f'类型{lt}')}] {text[:200]}" if text else f"[{TYPE_MAP.get(lt, f'类型{lt}')}]"


def _resolve_self_rowid(con, account_hint: str) -> Optional[int]:
    """在 Name2Id.user_name 里匹配本人账号 rowid(去掉可能的环境后缀)。"""
    cands = [account_hint]
    base = _SELF_ID_STRIP.split(account_hint)[0]
    if base and base != account_hint:
        cands.append(base)
    try:
        for name in cands:
            r = con.execute("SELECT rowid FROM Name2Id WHERE user_name=? LIMIT 1",
                            (name,)).fetchone()
            if r:
                return r[0]
    except Exception:
        pass
    return None


def fetch_messages(root: Path, contact: Contact, start: date, end: date,
                   account_hint: str = "") -> List[Message]:
    """取某联系人在 [start,end] 的消息。root 为解密后目录。"""
    # 消息库: message/message_0.db 或任意 message_*.db
    msg_dir = root / "message"
    msg_db = (msg_dir / "message_0.db") if (msg_dir / "message_0.db").exists() else None
    if msg_db is None or not msg_db.exists():
        cands = sorted(root.rglob("message_*.db"))
        cands = [c for c in cands if "fts" not in c.name.lower()]
        if not cands:
            raise RuntimeError("未找到 message_*.db 解密文件")
        msg_db = cands[0]
    md5 = hashlib.md5(contact.cid.lower().encode()).hexdigest()
    want = f"Msg_{md5}"
    try:
        con = sqlite3.connect(f"file:{msg_db.as_posix()}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as e:
        raise RuntimeError(f"无法打开消息库: {e}")
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        actual = next((t for t in tables if t.lower() == want.lower()), None)
        if not actual:
            return []
        start_ts = int(datetime.combine(start, time.min).timestamp())
        end_ts = int(datetime.combine(end, time(23, 59, 59)).timestamp())
        self_rowid = _resolve_self_rowid(con, account_hint)
        con.row_factory = sqlite3.Row
        sql = (
            f"SELECT m.local_type,m.create_time,m.message_content,"
            f"m.compress_content,m.real_sender_id,"
            f"n.user_name AS sender_username "
            f"FROM \"{actual}\" m "
            f"LEFT JOIN Name2Id n ON m.real_sender_id=n.rowid "
            f"WHERE m.create_time>=? AND m.create_time<=? "
            f"ORDER BY m.sort_seq ASC, m.local_id ASC")
        rows = con.execute(sql, (start_ts, end_ts)).fetchall()
        out = []
        for row in rows:
            rid = row["real_sender_id"]
            is_sent = bool(self_rowid and rid == self_rowid)
            ct = int(row["create_time"] or 0)
            if ct > 100000000000:
                ct //= 1000
            lt = int(row["local_type"] or 0)
            out.append(Message(
                ts=datetime.fromtimestamp(ct),
                sender=row["sender_username"] or "",
                text=_decode(row["compress_content"], row["message_content"], lt),
                is_self=is_sent,
                msg_type=TYPE_MAP.get(lt, f"type{lt}")))
        return out
    finally:
        con.close()
