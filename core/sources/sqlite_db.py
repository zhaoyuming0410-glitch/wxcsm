"""已解密微信数据库数据源（只读 SQLite）。

设计边界（重要，请务必读完）：
  本模块【不包含任何解密逻辑，也不会读取微信进程内存】。
  它要求用户先用外部工具（如 PyWxDump / WeChatMsg 留痕）把本地微信库导出为
  「已解密的 .db 文件」，本工具再以只读方式读取这些文件。

这样划分的原因：
  1) 内存扒密钥的手段会随微信版本更新而失效，绑进主流程会让工具随时变砖；
  2) 企业合规审查里，"读取已授权导出的数据文件" 比 "注入微信进程" 好解释得多；
  3) 责任边界清晰——取数授权由用户在外部工具中完成。

兼容性：完整支持微信 3.x（MicroMsg.db + MSG*.db），对 4.x 结构做尽力而为的探测。
探测失败时会明确提示改用「导入文件」数据源，而不是抛一堆看不懂的报错。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..models import Contact, Message
from .base import ChatSource, SourceError, resolve_zip_root

# 微信 3.x 消息类型 → 人类可读占位符。非文本消息保留占位符，避免丢失上下文语义。
TYPE_MAP = {
    1: "text", 3: "图片", 34: "语音", 43: "视频", 47: "表情",
    42: "名片", 48: "位置", 49: "文件/链接", 10000: "系统消息", 10002: "系统消息",
}

TIME_COLS = ("createtime", "create_time", "createTime")
CONTENT_COLS = ("strcontent", "message_content", "content")
TALKER_COLS = ("strtalker", "talker", "username", "user_name")
WXID_RE = re.compile(rb"[a-zA-Z][a-zA-Z0-9_-]{5,30}(?:@chatroom)?")
XML_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)


def _cols(con: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
    except sqlite3.Error:
        return []


def _tables(con: sqlite3.Connection) -> List[str]:
    try:
        return [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
    except sqlite3.Error:
        return []


def _find(cols: List[str], candidates: Tuple[str, ...]) -> Optional[str]:
    low = {c.lower(): c for c in cols}
    for cand in candidates:
        if cand.lower() in low:
            return low[cand.lower()]
    return None


def _open(path: Path) -> sqlite3.Connection:
    """以只读模式打开，杜绝任何写入可能。"""
    uri = f"file:{path.as_posix()}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True, timeout=10)


def _clean(text: str, msg_type: int) -> str:
    text = (text or "").strip()
    label = TYPE_MAP.get(msg_type, f"类型{msg_type}")
    if msg_type == 1:
        return text
    if msg_type == 49 and text.startswith("<"):
        m = XML_TITLE_RE.search(text)
        if m:
            return f"[{label}] {m.group(1).strip()}"
    if text.startswith("<"):
        return f"[{label}]"
    return f"[{label}] {text[:80]}" if text else f"[{label}]"


def _sender_from_blob(blob) -> str:
    """3.x 群聊真实发送人埋在 BytesExtra 的 protobuf 里。做轻量扫描，取不到就算了。"""
    if not blob:
        return ""
    try:
        for m in WXID_RE.finditer(bytes(blob)):
            s = m.group(0).decode("ascii", "ignore")
            if s.startswith("wxid_") or s.endswith("@chatroom"):
                return s
    except Exception:
        pass
    return ""


class DecryptedDbSource(ChatSource):
    key = "wxdb"
    label = "读取已解密的微信数据库文件（.db 文件夹）"
    hint = ("需先用 PyWxDump / WeChatMsg（留痕）/ WeChatDataAnalysis 等外部工具解密导出微信数据库，"
            "然后在此选择解密后的 .zip 压缩包（或解压后的文件夹，里面是 MicroMsg.db / MSG*.db 等）。"
            "本工具仅以只读方式读取，不解密、不读取微信进程内存。")
    needs_path = True
    path_kind = "file"
    path_label = "已解密数据库（.zip 或文件夹）"

    def __init__(self, **options):
        super().__init__(**options)
        self._msg_dbs: List[Tuple[Path, str, Dict[str, str]]] = []   # (库路径, 表名, 列映射)
        self._contacts: List[Contact] = []
        self._md5_index: Dict[str, str] = {}                          # md5(username) -> username
        self._scanned = False
        # 我方标识名：① 把「我方」消息显示为真实名字（默认「我」）；② 校正数据库 is_sender
        # 在群聊偶尔归错人的情况——解析出的发送人命中名单即判为我方。
        _sn = [str(s).strip() for s in (options.get("self_names") or []) if str(s).strip()]
        self._self_names: set = set(_sn)
        self._primary_self: str = _sn[0] if _sn else "我"

    @property
    def root(self) -> Path:
        p = str(self.options.get("path") or "").strip()
        if not p:
            raise SourceError("尚未选择已解密数据库（压缩包或文件夹）。")
        return resolve_zip_root(Path(p))

    # ---------- 自检 ----------
    def health(self) -> Tuple[bool, str]:
        try:
            root = self.root
        except SourceError as e:
            return False, str(e)
        if not root.exists():
            return False, f"路径不存在：{root}"
        dbs = list(root.rglob("*.db"))
        if not dbs:
            return False, f"该文件夹内没有 .db 文件：{root}"
        try:
            self._scan()
        except SourceError as e:
            return False, str(e)
        return True, f"已识别 {len(self._msg_dbs)} 张消息表、{len(self._contacts)} 个聊天对象"

    # ---------- 扫描 ----------
    def _scan(self) -> None:
        if self._scanned:
            return
        dbs = sorted(self.root.rglob("*.db"))
        contacts: Dict[str, Contact] = {}
        msg_tables: List[Tuple[Path, str, Dict[str, str]]] = []

        for db in dbs:
            try:
                con = _open(db)
            except sqlite3.Error:
                continue
            try:
                for tb in _tables(con):
                    cols = _cols(con, tb)
                    if not cols:
                        continue
                    low = tb.lower()
                    # 联系人表
                    if low in {"contact", "wacontact"}:
                        self._collect_contacts(con, tb, cols, contacts)
                        continue
                    # 消息表：按列特征识别，而不是硬编码表名
                    t_col = _find(cols, TIME_COLS)
                    c_col = _find(cols, CONTENT_COLS)
                    if not (t_col and c_col):
                        continue
                    mapping = {
                        "time": t_col,
                        "content": c_col,
                        "talker": _find(cols, TALKER_COLS) or "",
                        "type": _find(cols, ("type", "local_type", "MsgType")) or "",
                        "is_sender": _find(cols, ("issender", "is_sender")) or "",
                        "extra": _find(cols, ("bytesextra", "bytes_extra")) or "",
                        "table": tb,
                    }
                    if low.startswith("msg") or low.startswith("chat"):
                        msg_tables.append((db, tb, mapping))
            finally:
                con.close()

        if not msg_tables:
            raise SourceError(
                "没能在所选文件夹里识别出微信消息表。\n"
                "可能原因：数据库未解密、或微信版本结构不受支持。\n"
                "建议改用「导入已导出的聊天记录文件」数据源，稳定性更好。"
            )

        self._msg_dbs = msg_tables
        self._md5_index = {hashlib.md5(u.encode()).hexdigest().lower(): u for u in contacts}
        self._contacts = self._merge_counts(contacts)
        self._scanned = True

    def _collect_contacts(self, con, table: str, cols: List[str], out: Dict[str, Contact]) -> None:
        u = _find(cols, ("username", "user_name", "UserName"))
        if not u:
            return
        nick = _find(cols, ("nickname", "nick_name", "NickName")) or u
        remark = _find(cols, ("remark", "Remark", "alias", "Alias")) or u
        try:
            rows = con.execute(f'SELECT "{u}","{nick}","{remark}" FROM "{table}"').fetchall()
        except sqlite3.Error:
            return
        for uid, nk, rk in rows:
            uid = (uid or "").strip()
            if not uid or uid.startswith("gh_"):        # 过滤公众号
                continue
            name = (rk or "").strip() or (nk or "").strip() or uid
            out[uid] = Contact(
                cid=uid, name=name,
                kind="group" if uid.endswith("@chatroom") else "friend",
                alias=(nk or "").strip(),
            )

    def _merge_counts(self, contacts: Dict[str, Contact]) -> List[Contact]:
        """统计每个对象的消息数与最后活跃时间，让界面能按活跃度排序。"""
        stats: Dict[str, Tuple[int, Optional[datetime]]] = {}
        for db, tb, mp in self._msg_dbs:
            try:
                con = _open(db)
            except sqlite3.Error:
                continue
            try:
                if mp["talker"]:
                    sql = (f'SELECT "{mp["talker"]}", COUNT(1), MAX("{mp["time"]}") '
                           f'FROM "{tb}" GROUP BY 1')
                    for talker, cnt, mx in con.execute(sql):
                        if not talker:
                            continue
                        self._bump(stats, str(talker), cnt, mx)
                else:
                    # 4.x：对象编码在表名 Msg_<md5(username)> 里，反查 md5 索引
                    suffix = tb.split("_", 1)[-1].lower()
                    uid = self._md5_index.get(suffix)
                    if not uid:
                        continue
                    row = con.execute(
                        f'SELECT COUNT(1), MAX("{mp["time"]}") FROM "{tb}"').fetchone()
                    if row:
                        self._bump(stats, uid, row[0], row[1])
            except sqlite3.Error:
                continue
            finally:
                con.close()

        out: List[Contact] = []
        for uid, (cnt, last) in stats.items():
            base = contacts.get(uid)
            name = base.name if base else uid
            kind = base.kind if base else ("group" if uid.endswith("@chatroom") else "friend")
            alias = base.alias if base else ""
            out.append(Contact(cid=uid, name=name, kind=kind, alias=alias,
                               msg_count=cnt, last_time=last))
        # 有联系人记录但区间内无消息的，也列出来，避免用户以为"人不见了"
        for uid, c in contacts.items():
            if uid not in stats:
                out.append(c)
        return sorted(out, key=lambda c: (c.last_time or datetime.min), reverse=True)

    @staticmethod
    def _bump(stats, uid: str, cnt: int, mx) -> None:
        ts = None
        if mx:
            try:
                ts = datetime.fromtimestamp(int(mx))
            except (ValueError, OSError, OverflowError):
                ts = None
        old_cnt, old_ts = stats.get(uid, (0, None))
        stats[uid] = (old_cnt + int(cnt or 0), max(filter(None, [old_ts, ts]), default=None))

    # ---------- 接口实现 ----------
    def list_contacts(self) -> List[Contact]:
        self._scan()
        return self._contacts

    def fetch(self, contact: Contact, start: date, end: date) -> List[Message]:
        self._scan()
        lo, hi = self.day_bounds(start, end)
        lo_ts, hi_ts = int(lo.timestamp()), int(hi.timestamp())
        out: List[Message] = []
        target_md5 = hashlib.md5(contact.cid.encode()).hexdigest().lower()

        for db, tb, mp in self._msg_dbs:
            if not mp["talker"] and not tb.lower().endswith(target_md5):
                continue
            cols = [mp["time"], mp["content"]]
            for key in ("type", "is_sender", "extra"):
                if mp[key]:
                    cols.append(mp[key])
            sel = ", ".join(f'"{c}"' for c in cols)
            where = f'"{mp["time"]}" BETWEEN ? AND ?'
            params: List = [lo_ts, hi_ts]
            if mp["talker"]:
                where += f' AND "{mp["talker"]}" = ?'
                params.append(contact.cid)
            try:
                con = _open(db)
            except sqlite3.Error:
                continue
            try:
                rows = con.execute(
                    f'SELECT {sel} FROM "{tb}" WHERE {where} ORDER BY "{mp["time"]}" ASC',
                    params,
                ).fetchall()
            except sqlite3.Error:
                continue
            finally:
                con.close()

            for row in rows:
                data = dict(zip(cols, row))
                try:
                    ts = datetime.fromtimestamp(int(data[mp["time"]]))
                except (ValueError, TypeError, OSError, OverflowError):
                    continue
                mtype = int(data.get(mp["type"]) or 1) if mp["type"] else 1
                text = _clean(str(data.get(mp["content"]) or ""), mtype)
                if not text:
                    continue
                is_self = bool(int(data.get(mp["is_sender"]) or 0)) if mp["is_sender"] else False
                sender = "我" if is_self else (
                    _sender_from_blob(data.get(mp["extra"])) if mp["extra"] else ""
                ) or contact.name
                # 二次校正：数据库 is_sender 在群聊偶尔标错；解析出的发送人命中我方标识名即判我方
                self_name = None
                if not is_self and self._self_names and sender in self._self_names:
                    is_self = True
                    self_name = sender
                if is_self:
                    sender = self_name or self._primary_self   # 我方统一显示为设置的标识名（默认「我」）
                out.append(Message(ts=ts, sender=sender, text=text, is_self=is_self,
                                   msg_type=TYPE_MAP.get(mtype, "text")))
        return self.sort_messages(out)
