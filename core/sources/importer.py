"""导入型数据源：读取已导出的聊天记录文件（CSV / TXT / JSON）。

这是最稳的一条路——不碰微信进程、不依赖微信版本、企业合规上说得清。
用户先用留痕 / WeChatMsg 等工具把聊天记录导出成文件，本工具只负责读文件。

约定：一个文件（或一个子目录）= 一个聊天对象，文件名即客户名称。
支持格式自动识别：
  *.csv   —— 表头自动映射（时间/发送人/内容/是否本人 的常见列名都认）
  *.json  —— 数组，元素含 时间 + 内容 字段
  *.txt   —— 形如 "2026-08-20 10:23:45 张三:\n消息内容"，支持多行续行
"""

from __future__ import annotations

import csv
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..models import Contact, Message
from .base import ChatSource, SourceError, resolve_zip_root

SUFFIXES = {".csv", ".txt", ".json"}

TIME_KEYS = ("strtime", "createtime", "time", "时间", "发送时间", "date", "datetime", "ts")
# 发送人别名：覆盖常见导出工具（留痕 / WeChatMsg / WeChatDataAnalysis 等）。
# senderDisplayName 优先（人类可读名），其次 senderUsername（wxid）。
SENDER_KEYS = ("senderdisplayname", "sendername", "sender", "displayname", "nickname",
               "fromusername", "talker", "from", "senderusername", "wxid",
               "remark", "发送人", "发言人", "昵称", "speaker", "name")
CONTENT_KEYS = ("strcontent", "content", "msg", "message", "内容", "消息", "text", "正文")
# 我方标记：WeChatDataAnalysis 用 isSent（布尔）；其余工具常见 issender / is_self 等。
SELF_KEYS = ("issent", "issend", "issender", "is_self", "self", "是否本人", "是否自己")
TYPE_KEYS = ("type", "msgtype", "类型")

# 发送人字面量里常见的「我方」写法。导出工具/用户手动整理时，公司侧常被标成
# 「我方 / 自己 / 本人 / 本号」等，必须识别成 self，否则「对接进度」段会落空。
SELF_NAMES = {"我", "self", "me", "myself", "自己", "本人", "我方", "本号"}

TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M",
    "%Y-%m-%dT%H:%M:%S", "%Y年%m月%d日 %H:%M:%S", "%Y年%m月%d日 %H:%M",
    "%Y-%m-%d", "%Y/%m/%d",
)

TXT_HEAD = re.compile(
    r"^\s*[\[\(]?"
    r"(?P<ts>\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?[ T]\d{1,2}:\d{2}(:\d{2})?)"
    r"[\]\)]?\s*(?P<sender>[^:：]{0,40}?)\s*[:：]?\s*$"
)
TXT_INLINE = re.compile(
    r"^\s*[\[\(]?"
    r"(?P<ts>\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?[ T]\d{1,2}:\d{2}(:\d{2})?)"
    r"[\]\)]?\s+(?P<sender>[^:：]{1,40}?)[:：]\s*(?P<body>.*)$"
)


def parse_time(value) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    if re.fullmatch(r"\d{10}", s):                      # 秒级时间戳
        return datetime.fromtimestamp(int(s))
    if re.fullmatch(r"\d{13}", s):                      # 毫秒级时间戳
        return datetime.fromtimestamp(int(s) / 1000)
    s = s.replace("年", "-").replace("月", "-").replace("日", "")
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _pick(row: Dict[str, str], keys: Tuple[str, ...]) -> Optional[str]:
    norm = {(k or "").strip().lower().replace(" ", ""): v for k, v in row.items()}
    for k in keys:
        if k in norm and str(norm[k]).strip() != "":
            return norm[k]
    return None


def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1", "true", "yes", "y", "是", "我", "self"}


class ImportSource(ChatSource):
    key = "import"
    label = "导入已导出的聊天记录文件（推荐 · 稳定合规）"
    hint = ("支持两种：① 直接选 WeChatDataAnalysis / 留痕 等工具导出的 .zip 压缩包，本工具自动解压并解析；"
            "② 选已解压的文件夹（每个 .csv/.txt/.json 文件对应一个聊天对象，文件名即客户名称）。")
    needs_path = True
    path_kind = "file"
    path_label = "聊天记录（.zip 或文件夹）"

    # ---------- 生命周期 ----------
    def __init__(self, **options):
        super().__init__(**options)
        self._cache: Dict[str, List[Message]] = {}
        # 内置的「我方」写法 + 用户显式指定的发送人名（真实姓名场景，如导出里公司侧标的是销售本人）。
        self.self_names = set(SELF_NAMES)
        for s in (self.options.get("self_names") or []):
            s = str(s).strip()
            if s:
                self.self_names.add(s)

    @property
    def root(self) -> Path:
        p = str(self.options.get("path") or "").strip()
        if not p:
            raise SourceError("尚未选择聊天记录（压缩包或文件夹）。")
        return resolve_zip_root(Path(p))

    def health(self) -> Tuple[bool, str]:
        try:
            root = self.root
        except SourceError as e:
            return False, str(e)
        if not root.exists():
            return False, f"路径不存在：{root}"
        # 误把「解密数据库存档」（.db）丢进导入源时，给出明确指引
        if any(root.rglob("*.db")):
            return False, ("这个压缩包/文件夹里是解密后的微信数据库（.db 文件），不是聊天记录文件。\n"
                           "请在第 ① 步把数据源切到「读取已解密的微信数据库文件（.db 文件夹）」再加载。")
        files = self._files()
        if not files:
            return False, f"该路径内没有找到 .csv / .txt / .json 文件：{root}"
        return True, f"已识别 {len(files)} 个聊天记录文件"

    def _files(self) -> List[Path]:
        root = self.root
        if root.is_file():
            return [root]
        out = [p for p in sorted(root.rglob("*")) if p.is_file() and p.suffix.lower() in SUFFIXES]
        return out

    # ---------- 接口实现 ----------
    def _conversation_inputs(self) -> List[Tuple[Path, str]]:
        """返回 [(messages.json 路径, 显示名称), ...]。

        兼容两种布局：
          A. WeChatDataAnalysis 导出：任意深度下 conversations/<id>/messages.json，
             会话名在 messages.json 的 conversation.displayName，且会附带 meta.json /
             manifest.json / report.json 等非会话文件（必须忽略）。
             注意 zip 解压后往往多一层 wechat_chat_export_xxx/ 包装目录，所以这里
             用 rglob 在根下递归找 conversations/，而不是只认 root/conversations。
          B. 扁平布局（原行为）：根目录下每个 .csv/.txt/.json 文件即一个聊天对象，
             文件名即客户名称。
        """
        root = self.root
        results: List[Tuple[Path, str]] = []
        for conv_root in sorted(root.rglob("conversations")):
            if not conv_root.is_dir():
                continue
            for sub in sorted(conv_root.iterdir()):
                if not sub.is_dir():
                    continue
                mj = sub / "messages.json"
                if not mj.is_file():
                    mj = sub / f"{sub.name}.json"
                    if not mj.is_file():
                        continue
                results.append((mj, self._read_conv_name(mj)))
        if results:
            return results
        return [(f, f.stem) for f in self._files()]

    @staticmethod
    def _read_conv_name(mj: Path) -> str:
        """从 messages.json 的 conversation 字段读取真实会话名，回退到目录名。"""
        try:
            data = json.loads(ImportSource._read_text(mj))
        except Exception:
            data = None
        if isinstance(data, dict):
            conv = data.get("conversation")
            if isinstance(conv, dict):
                for k in ("displayName", "name", "nickName", "remark", "username"):
                    v = conv.get(k)
                    if isinstance(v, str) and v.strip():
                        return v.strip()
        name = mj.parent.name
        name = re.sub(r"^\d+_", "", name)            # 去 0001_ 序号前缀
        name = re.sub(r"_[0-9a-fA-F]{8,}$", "", name)  # 去 _e1aab413 后缀
        return name or mj.stem

    def list_contacts(self) -> List[Contact]:
        out: List[Contact] = []
        for f, name in self._conversation_inputs():
            msgs = self._load(f)
            if not msgs:
                continue
            low = name.lower()
            kind = "group" if ("群" in name or "chatroom" in low or "@" in low) else "friend"
            out.append(
                Contact(
                    cid=str(f),
                    name=name,
                    kind=kind,
                    alias="",
                    msg_count=len(msgs),
                    last_time=msgs[-1].ts,
                )
            )
        if not out:
            raise SourceError("文件都读到了，但没能解析出任何消息。请检查文件格式，或改用内置样例数据先验证流程。")
        return sorted(out, key=lambda c: c.last_time or datetime.min, reverse=True)

    def fetch(self, contact: Contact, start: date, end: date) -> List[Message]:
        msgs = self._load(Path(contact.cid))
        lo, hi = self.day_bounds(start, end)
        return [m for m in msgs if lo <= m.ts <= hi]

    # ---------- 解析 ----------
    def _load(self, path: Path) -> List[Message]:
        key = str(path)
        if key in self._cache:
            return self._cache[key]
        try:
            suf = path.suffix.lower()
            if suf == ".csv":
                msgs = self._load_csv(path)
            elif suf == ".json":
                msgs = self._load_json(path)
            else:
                msgs = self._load_txt(path)
        except Exception as e:
            raise SourceError(f"解析文件失败：{path.name} —— {e}") from e
        msgs = self.sort_messages(msgs)
        self._cache[key] = msgs
        return msgs

    @staticmethod
    def _read_text(path: Path) -> str:
        for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030", "latin-1"):
            try:
                return path.read_text(encoding=enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return path.read_bytes().decode("utf-8", errors="replace")

    def _load_csv(self, path: Path) -> List[Message]:
        text = self._read_text(path)
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        out: List[Message] = []
        for row in csv.DictReader(text.splitlines(), dialect=dialect):
            ts = parse_time(_pick(row, TIME_KEYS))
            content = _pick(row, CONTENT_KEYS)
            if ts is None or not content:
                continue
            sender = _pick(row, SENDER_KEYS) or ""
            is_self = _truthy(_pick(row, SELF_KEYS) or "") or sender.strip() in self.self_names
            out.append(
                Message(ts=ts, sender=sender.strip(), text=str(content).strip(),
                        is_self=is_self, msg_type=str(_pick(row, TYPE_KEYS) or "text"))
            )
        return out

    def _load_json(self, path: Path) -> List[Message]:
        data = json.loads(self._read_text(path))
        if isinstance(data, dict):
            for k in ("messages", "data", "list", "msgs"):
                if isinstance(data.get(k), list):
                    data = data[k]
                    break
        if not isinstance(data, list):
            raise ValueError("JSON 顶层需要是数组，或含 messages/data/list 数组字段")
        out: List[Message] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            row = {str(k): v for k, v in item.items()}
            ts = parse_time(_pick(row, TIME_KEYS))
            content = _pick(row, CONTENT_KEYS)
            if ts is None or not content:
                continue
            sender = str(_pick(row, SENDER_KEYS) or "").strip()
            is_self = _truthy(_pick(row, SELF_KEYS) or "") or sender in self.self_names
            out.append(Message(ts=ts, sender=sender, text=str(content).strip(), is_self=is_self))
        return out

    def _load_txt(self, path: Path) -> List[Message]:
        out: List[Message] = []
        cur: Optional[Message] = None
        for raw_line in self._read_text(path).splitlines():
            line = raw_line.rstrip()
            m = TXT_INLINE.match(line)
            if m:
                ts = parse_time(m.group("ts"))
                if ts:
                    if cur:
                        out.append(cur)
                    sender = m.group("sender").strip()
                    cur = Message(ts=ts, sender=sender, text=m.group("body").strip(),
                                  is_self=sender in self.self_names)
                    continue
            m = TXT_HEAD.match(line)
            if m:
                ts = parse_time(m.group("ts"))
                if ts:
                    if cur:
                        out.append(cur)
                    sender = (m.group("sender") or "").strip()
                    cur = Message(ts=ts, sender=sender, text="", is_self=sender in self.self_names)
                    continue
            if cur is not None and line.strip():
                cur.text = (cur.text + "\n" + line.strip()).strip()
        if cur:
            out.append(cur)
        return [m for m in out if m.text]
