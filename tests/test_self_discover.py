# -*- coding: utf-8 -*-
"""「我方成员」自动发现：回归测试。

这里的核心风险是**把客户推荐成我方同事**——猜错了会把客户的话当成我方答复，
污染整张问答表，而且比漏掉更难发现。所以重点测"不该推的别推"。

直接运行：python tests/test_self_discover.py
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.models import ChatBundle, Contact, Message
from core.self_discover import suggest_self_members


def m(hh, mm, who, text, is_self=False, sid=None):
    return Message(ts=datetime(2026, 8, 5, hh, mm), sender=who, text=text,
                   is_self=is_self, sender_id=(sid if sid is not None else who))


class TestSuggestSelf(unittest.TestCase):
    def test_企微桥接号被优先推荐(self):
        msgs = [
            m(9, 0, "张三", "审批到第二级卡住了怎么办？"),
            m(9, 5, "张三", "我看一下", sid="10000000000000001@openim"),
            m(9, 30, "张三", "有结果了吗？"),
            m(9, 35, "张三", "在配了", sid="10000000000000001@openim"),
            m(10, 0, "张三", "配好了", sid="10000000000000001@openim"),
        ]
        out = suggest_self_members(msgs)
        self.assertTrue(out)
        self.assertEqual(out[0].name, "张三")
        self.assertIn("桥接号", "；".join(out[0].reasons))

    def test_自己不算候选(self):
        msgs = [m(9, 0, "我", "我这边看一下", is_self=True) for _ in range(5)]
        self.assertEqual(suggest_self_members(msgs), [])

    def test_已配置的我方成员不再作为候选(self):
        msgs = [m(9, i, "张三", "我这边处理一下", sid="x@openim") for i in range(5)]
        self.assertEqual(suggest_self_members(msgs, known_self=["张三"]), [])

    def test_发言太少不推(self):
        msgs = [m(9, 0, "路人", "收到", sid="wxid_luren")]
        self.assertEqual(suggest_self_members(msgs, min_msgs=3), [])

    def test_客户互相问答不会被误推成同事(self):
        # 群里客户之间会互相解答，这种"接话"不能算我方证据。
        # 这里两个客户都是"接话且自己在提问"，不能拿到回应分。
        msgs = []
        for i in range(4):
            msgs.append(m(9, i * 10, "客户甲", "这个流程要怎么走？", sid="wxid_jia"))
            msgs.append(m(9, i * 10 + 1, "客户乙", "你们那边是怎么配的呢？", sid="wxid_yi"))
        out = {c.name: c for c in suggest_self_members(msgs)}
        self.assertIn("客户甲", out)
        self.assertEqual(out["客户甲"].reply_count, 0)
        self.assertEqual(out["客户乙"].reply_count, 0)
        self.assertEqual(out["客户甲"].score, 0)

    def test_同一人的两个号分别列出(self):
        # 展示名不同、账号不同的两个号应各自成为候选，方便用户都填进配置
        msgs = ([m(9, i, "张三", "我这边看", sid="10000000000000001@openim")
                 for i in range(3)]
                + [m(10, i, "张三 交付", "好的", sid="wxid_zhangsan")
                   for i in range(3)])
        out = suggest_self_members(msgs)
        cids = {c.cid for c in out}
        self.assertEqual(cids, {"10000000000000001@openim", "wxid_zhangsan"})

    def test_展示名撞车时按账号分开统计(self):
        # 两个不同账号备注撞名，不能被合并成一个人
        msgs = ([m(9, i, "李四", "我这边查", sid="wxid_a") for i in range(3)]
                + [m(10, i, "李四", "我这边查", sid="wxid_b") for i in range(3)])
        out = suggest_self_members(msgs)
        self.assertEqual(len(out), 2)
        self.assertEqual({c.msg_count for c in out}, {3})

    def test_我方口吻加分且给出理由(self):
        msgs = ([m(9, 0, "张三", "出问题了吗？")]
                + [m(9, i + 1, "李工", "我这边同步一下", sid="wxid_wang") for i in range(3)])
        out = {c.name: c for c in suggest_self_members(msgs)}
        self.assertIn("李工", out)
        self.assertTrue(out["李工"].commit_count >= 1)
        self.assertTrue(any("我方口吻" in r for r in out["李工"].reasons))

    def test_建议值就是展示名(self):
        msgs = [m(9, i, "张三 交付", "我这边看", sid="wxid_x") for i in range(3)]
        self.assertEqual(suggest_self_members(msgs)[0].suggestion, "张三 交付")


if __name__ == "__main__":
    unittest.main(verbosity=2)
