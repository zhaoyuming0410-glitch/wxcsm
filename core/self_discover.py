# -*- coding: utf-8 -*-
"""「我方成员」自动发现 —— 帮用户把 self_names 填对。

为什么需要这个：
    群聊里"我方"往往不止账号本人。同事用自己的号或企微/OpenIM 桥接号发言时，
    默认判定会把它当成"对方"，问答清单的答复就会大面积变成「未答复」。
    但要用户自己去查 wxid、或者回忆群里哪几个号是同事，门槛太高。

做法：**给候选和理由，让人来拍板，不自动替人决定。**
    判定"谁是我方"本质上是个业务问题（谁是同事），猜错了会把客户的话当成
    我方答复、污染整张表，比漏掉更难发现。所以这里只做排序和解释，
    最终由用户确认后写进配置。

    评分只看"像不像在替我们说话"，每条都给得出理由，能被人核对。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set

from .models import Message

# 企微/OpenIM ↔ 微信 的桥接账号：形如 10000000000000001@openim。
# 这类号在客户群里几乎总是自家同事（客户方一般用个人微信号），是很强的信号。
_OPENIM = re.compile(r"@openim$", re.I)
# 我方口吻：第一人称主动认领 / 承诺动作
_COMMIT = re.compile(
    r"我(?:这边|们这边)?(?:来|去|先|帮|给|拉|发|问|查|看|安排|同步|确认|提|约|跟|处理|对接|反馈)"
    r"|我(?:今天|明天|后天|稍后|稍晚|下午|上午|本周|下周)")
# 直接回应客户的迹象：紧跟在一句问句之后发言
_QUESTION_TAIL = re.compile(r"[?？]$|(?:吗|呢|么|吧)[?？]?$")


@dataclass
class Candidate:
    """一个"可能是我方"的群成员，附评分依据。"""

    name: str
    cid: str
    msg_count: int = 0
    reply_count: int = 0          # 紧接着客户问句之后发言的次数
    commit_count: int = 0         # 第一人称主动认领/承诺的次数
    score: int = 0
    reasons: List[str] = field(default_factory=list)

    @property
    def suggestion(self) -> str:
        """可直接粘进 --self-names 的值。"""
        return self.name

    def describe(self) -> str:
        return (f"{self.name}（{self.cid}）发言 {self.msg_count} 次"
                f"，{'；'.join(self.reasons)}")


def suggest_self_members(messages: Sequence[Message],
                         known_self: Optional[Sequence[str]] = None,
                         owner_names: Optional[Sequence[str]] = None,
                         min_msgs: int = 3,
                         top: int = 15) -> List[Candidate]:
    """从一段聊天记录里挑出"可能是我方同事"的人，按可信度排序。

    known_self  —— 已配置的我方名字，这些人不再作为候选
    owner_names —— 账号本人的发送人名（wx4 源里就是本人账号名），排除掉
    """
    known = {s.strip() for s in (known_self or []) if s and s.strip()}
    owners = {s.strip() for s in (owner_names or []) if s and s.strip()}

    def key_of(m: Message) -> str:
        """按原始账号归并，而不是展示名——两个人备注可能撞名，账号不会。"""
        return (m.sender_id or m.sender or "").strip()

    def is_known(sender: str) -> bool:
        return any(k in sender for k in known)

    stats: Dict[str, Candidate] = {}
    order: List[str] = []
    for m in messages:
        if m.is_self:
            continue                     # 已经是"我方"的不用猜
        k = key_of(m)
        who = (m.sender or "").strip()
        if not k or who in owners or is_known(who):
            continue
        if k not in stats:
            stats[k] = Candidate(name=who or k, cid=k)
            order.append(k)
        stats[k].msg_count += 1

    if not stats:
        return []

    # 逐条看"谁在回答客户的问句"：某条消息如果紧跟在一条以问号/吗/呢结尾的
    # 他人消息之后，就算一次回应。这是"在替我们答话"最直接的行为证据。
    for i, m in enumerate(messages):
        if m.is_self:
            continue
        k = key_of(m)
        if k not in stats:
            continue
        text = (m.text or "").strip()
        if _COMMIT.search(text):
            stats[k].commit_count += 1
        prev = None
        for j in range(i - 1, -1, -1):
            if key_of(messages[j]) != k or messages[j].is_self:
                prev = messages[j]
                break
        if prev is not None:
            ptxt = (prev.text or "").strip()
            # 只在"对方刚才在提问、这个人接话且自己不是在提问"时算回应。
            # 「自己也是在提问」这一条很关键：群里客户之间会互相问答，
            # 不加这个过滤会把爱发言的客户也推荐成我方同事。
            if (not prev.is_self and _QUESTION_TAIL.search(ptxt)
                    and not _QUESTION_TAIL.search(text)):
                stats[k].reply_count += 1

    out: List[Candidate] = []
    for k in order:
        c = stats[k]
        if c.msg_count < min_msgs:
            continue
        # 评分：桥接号最强，其次是"在回答客户问题"的行为证据，最后是口吻
        if _OPENIM.search(c.cid):
            c.score += 60
            c.reasons.append("企微/OpenIM 桥接号（通常是自家同事）")
        if c.reply_count:
            # 回应占比越高越像"服务方"，但绝对次数也要够
            ratio = c.reply_count / max(1, c.msg_count)
            c.score += min(30, c.reply_count * 3) + int(ratio * 20)
            c.reasons.append(f"{c.reply_count} 次紧跟在客户问句之后发言")
        if c.commit_count:
            c.score += min(20, c.commit_count * 2)
            c.reasons.append(f"{c.commit_count} 次用我方口吻主动认领/承诺")
        if not c.reasons:
            c.reasons.append("仅发言较多，没有明显我方迹象")
        out.append(c)

    out.sort(key=lambda x: (-x.score, -x.msg_count, x.name))
    return out[:top]
