"""核心数据模型。

整个工具围绕四个概念运转：
  Contact    —— 一个聊天对象（微信好友 或 微信群）
  Message    —— 一条聊天消息
  ChatBundle —— 一个聊天对象 + 一个时间范围 内的全部消息（总结的最小单位）
  Summary    —— 一篇沟通总结，恰好对应 Excel 的一行
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import List, Optional

KIND_LABEL = {"friend": "好友", "group": "群聊", "unknown": "未知"}


@dataclass(frozen=True)
class Contact:
    """一个聊天对象。cid 是数据源内部唯一标识（如 wxid / 群 id / 文件名）。"""

    cid: str
    name: str
    kind: str = "unknown"          # friend | group | unknown
    alias: str = ""                # 备注名 / 原始昵称
    msg_count: int = 0             # 已知消息总数，0 表示未统计
    last_time: Optional[datetime] = None

    @property
    def display(self) -> str:
        extra = f"（{self.alias}）" if self.alias and self.alias != self.name else ""
        tail = f"  ·  {self.msg_count} 条" if self.msg_count else ""
        return f"[{KIND_LABEL.get(self.kind, '未知')}] {self.name}{extra}{tail}"

    def matches(self, keyword: str) -> bool:
        """搜索匹配：昵称、备注、内部 id 任一命中即可（忽略大小写）。"""
        if not keyword:
            return True
        kw = keyword.strip().lower()
        return any(kw in (v or "").lower() for v in (self.name, self.alias, self.cid))


@dataclass
class Message:
    ts: datetime
    sender: str
    text: str
    is_self: bool = False
    msg_type: str = "text"         # text | image | file | voice | system ...

    def line(self) -> str:
        who = "我" if self.is_self else (self.sender or "对方")
        body = self.text if self.msg_type == "text" else f"[{self.msg_type}] {self.text}".strip()
        return f"{self.ts:%m-%d %H:%M} {who}：{body}"


@dataclass
class ChatBundle:
    """总结的最小单位：一个聊天对象 + 一个时间范围。"""

    contact: Contact
    start: date
    end: date
    messages: List[Message] = field(default_factory=list)

    @property
    def time_range(self) -> str:
        return f"{self.start:%Y-%m-%d} ~ {self.end:%Y-%m-%d}"

    @property
    def count(self) -> int:
        return len(self.messages)

    def transcript(self, max_chars: int = 12000) -> str:
        """拼成可喂给模型的纯文本。超长时保留首尾——开头交代背景，结尾是最新进度。"""
        lines = [m.line() for m in self.messages]
        text = "\n".join(lines)
        if len(text) <= max_chars:
            return text
        head_budget = int(max_chars * 0.4)
        tail_budget = max_chars - head_budget
        head, tail, used = [], [], 0
        for ln in lines:
            if used + len(ln) > head_budget:
                break
            head.append(ln)
            used += len(ln) + 1
        used = 0
        for ln in reversed(lines):
            if used + len(ln) > tail_budget:
                break
            tail.append(ln)
            used += len(ln) + 1
        tail.reverse()
        return "\n".join(head) + "\n……（中间省略部分消息）……\n" + "\n".join(tail)


@dataclass
class Summary:
    """一行 Excel。字段刻意只留三个业务列，其余为运行期元数据，不导出。"""

    customer_name: str
    time_range: str
    content: str
    msg_count: int = 0
    engine: str = ""               # ai / offline，仅用于界面提示
    error: str = ""

    @property
    def char_len(self) -> int:
        return len(re.sub(r"\s", "", self.content))
