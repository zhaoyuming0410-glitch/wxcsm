# -*- coding: utf-8 -*-
"""客户问题答复清单 AI 引擎：解析健壮性与降级路径的回归测试。

不联网——把 `_chat` 换成假实现，只验证"模型不听话时我们还能不能活"。

直接运行：python tests/test_qa_ai.py
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import qa_ai, qa_extract
from core.models import (
    QA_STATUS_DONE, QA_STATUS_PENDING, QA_STATUS_PROMISED, ChatBundle, Contact, Message,
)
from core.summarizer import LLMError

BOT = "壬癸客服"


def mk(messages):
    msgs = []
    for i, (d, hh, mm, who, text) in enumerate(messages):
        msgs.append(Message(ts=datetime(2026, 8, d, hh, mm), sender=who, text=text,
                            is_self=(who == BOT)))
    return ChatBundle(contact=Contact(cid="c1", name="测试客户", kind="friend"),
                      start=date(2026, 8, 1), end=date(2026, 8, 31), messages=msgs)


class TestParseJson(unittest.TestCase):
    def test_裸数组(self):
        self.assertEqual(len(qa_ai._parse_json_array('[{"类型":"提问","问题序号":1}]')), 1)

    def test_代码块围栏(self):
        s = '```json\n[{"a":1},{"b":2}]\n```'
        self.assertEqual(len(qa_ai._parse_json_array(s)), 2)

    def test_前后有寒暄(self):
        s = '好的，以下是整理结果：\n[{"a":1},{"b":2}]\n希望有帮助。'
        self.assertEqual(len(qa_ai._parse_json_array(s)), 2)

    def test_多余逗号(self):
        self.assertEqual(len(qa_ai._parse_json_array('[{"a":1},{"b":2},]')), 2)

    def test_空数组是合法的(self):
        self.assertEqual(qa_ai._parse_json_array("[]"), [])

    def test_非对象元素被丢掉(self):
        self.assertEqual(len(qa_ai._parse_json_array('[{"a":1}, "垃圾", 3]')), 1)

    def test_垃圾输入必须抛错而不是悄悄返回空(self):
        # 这条很重要：解析失败要降级到离线，不能当成"确实没有内容"而输出空表
        for bad in ("没有数组", "", "   ", "null"):
            with self.assertRaises(LLMError, msg=bad):
                qa_ai._parse_json_array(bad)


class TestAsInt(unittest.TestCase):
    def test_各种写法(self):
        self.assertEqual(qa_ai._as_int(12), 12)
        self.assertEqual(qa_ai._as_int("#12"), 12)
        self.assertEqual(qa_ai._as_int("12"), 12)
        self.assertEqual(qa_ai._as_int(12.0), 12)
        self.assertEqual(qa_ai._as_int("  #7 "), 7)

    def test_无效值返回None(self):
        for bad in (None, "abc", "", True, False, [], {}):
            self.assertIsNone(qa_ai._as_int(bad), bad)


class TestBuildItem(unittest.TestCase):
    def setUp(self):
        self.b = mk([
            (5, 9, 12, "张三", "审批到第二级卡住了，说没有匹配的审批人"),
            (5, 9, 20, BOT, "收到，是审批流里海南分组没绑对应审批节点，我今晚修"),
        ])
        self.entries = qa_extract.prepare_entries(self.b)
        self.by = {i: (m, c) for i, m, c in self.entries}

    def build(self, rec):
        return qa_ai._build_item(rec, self.b, self.by, 80, 150)

    def test_序号有效时时间与原始记录来自真实消息(self):
        it = self.build({"类型": "报错", "问题序号": 0, "答复序号": 1,
                         "客户问题": "审批到第二级卡住", "我方答复": "海南分组没绑审批节点",
                         "状态": "已答复"})
        self.assertEqual(it.kind, "报错")
        self.assertEqual(it.status, QA_STATUS_DONE)
        self.assertEqual(it.ask_time, "08-05 09:12")      # 真实消息时间，模型没机会编
        self.assertEqual(it.answer_time, "08-05 09:20")
        self.assertIn("客户[08-05 09:12]：审批到第二级卡住了", it.raw)
        self.assertIn("我方[08-05 09:20]：收到，是审批流里海南分组", it.raw)

    def test_序号不存在就丢掉(self):
        # 宁可少一行，也不能凭空造一条没有时间、没有原话的记录
        self.assertIsNone(self.build({"类型": "提问", "问题序号": 999,
                                      "客户问题": "编的", "我方答复": "编的"}))
        self.assertIsNone(self.build({"类型": "提问", "问题序号": None,
                                      "客户问题": "编的"}))

    def test_没有答复就是未答复且答复时间占位(self):
        it = self.build({"类型": "提问", "问题序号": 0, "答复序号": None,
                         "客户问题": "审批到第二级卡住", "我方答复": "模型硬凑的答复"})
        self.assertEqual(it.status, QA_STATUS_PENDING)
        self.assertEqual(it.answer, "")            # 模型硬凑的答复不能留
        self.assertEqual(it.answer_time, "—")

    def test_我方待办用答复序号定位且问题列占位(self):
        it = self.build({"类型": "我方待办", "问题序号": None, "答复序号": 1,
                         "客户问题": "", "我方答复": "海南分组我今天配好"})
        self.assertEqual(it.kind, "我方待办")
        self.assertEqual(it.status, QA_STATUS_PROMISED)
        self.assertEqual(it.question_cell, "—")
        self.assertEqual(it.ask_time, "08-05 09:20")
        # 还没被答复，答复时间必须留空，否则会让人误以为已闭环
        self.assertEqual(it.answer_time, "—")
        self.assertIn("我方[08-05 09:20]", it.raw)

    def test_我方待办的时间口径与离线引擎一致(self):
        # 两条引擎对同一件事必须给出一致的列值，否则用户切换引擎会看到表格变样
        rec = {"类型": "我方待办", "问题序号": None, "答复序号": 1,
               "客户问题": "", "我方答复": "海南分组我今天配好"}
        ai = self.build(rec)
        self.assertEqual(ai.answer_time, "—")
        self.assertEqual(ai.question_cell, "—")
        self.assertEqual(ai.status, QA_STATUS_PROMISED)

    def test_描述未答复的话不能当成答复(self):
        # 实测模型会输出「（未直接答复，客户表示了解）」，若不处理会得到
        # 「已答复：未直接答复」这种自相矛盾的行，还会掩盖真实的服务缺口
        for meta in ("（未直接答复）", "（未直接答复，客户表示了解）",
                     "无明确答复", "未提及", "暂无"):
            it = self.build({"类型": "提问", "问题序号": 0, "答复序号": 1,
                             "客户问题": "审批到第二级卡住", "我方答复": meta})
            self.assertEqual(it.status, QA_STATUS_PENDING, meta)
            self.assertEqual(it.answer, "", meta)
            self.assertEqual(it.answer_time, "—", meta)

    def test_正常答复里含未字不会误杀(self):
        it = self.build({"类型": "提问", "问题序号": 0, "答复序号": 1,
                         "客户问题": "审批到第二级卡住",
                         "我方答复": "海南分组未绑定审批节点，已让实施同事补上，明天生效"})
        self.assertEqual(it.status, QA_STATUS_DONE)
        self.assertTrue(it.answer)

    def test_类型非法时回落成提问(self):
        it = self.build({"类型": "随便写的", "问题序号": 0, "答复序号": 1,
                         "客户问题": "审批到第二级卡住", "我方答复": "海南分组没绑"})
        self.assertEqual(it.kind, "提问")

    def test_客户问题为空就丢掉(self):
        self.assertIsNone(self.build({"类型": "提问", "问题序号": 0, "答复序号": 1,
                                      "客户问题": "  ", "我方答复": "x"}))

    def test_超长内容按配置截断(self):
        it = qa_ai._build_item(
            {"类型": "提问", "问题序号": 0, "答复序号": 1,
             "客户问题": "甲" * 200, "我方答复": "乙" * 300},
            self.b, self.by, 80, 150)
        self.assertLessEqual(len(it.question), 81)     # 80 + 省略号
        self.assertLessEqual(len(it.answer), 151)


class TestExtractQaAi(unittest.TestCase):
    def setUp(self):
        self.b = mk([
            (5, 9, 12, "张三", "审批到第二级卡住了，说没有匹配的审批人"),
            (5, 9, 20, BOT, "收到，是审批流里海南分组没绑对应审批节点，我今晚修"),
            (6, 10, 0, "张三", "培训能安排吗"),
        ])

    def run_with(self, payload):
        with mock.patch.object(qa_ai, "_chat", return_value=payload):
            return qa_ai.extract_qa_ai(self.b, {"llm_api_key": "x", "llm_base_url": "http://x"})

    def test_正常抽取(self):
        items = self.run_with(
            '[{"类型":"报错","问题序号":0,"答复序号":1,'
            '"客户问题":"审批卡在第二级","我方答复":"海南分组没绑审批节点"},'
            '{"类型":"需求","问题序号":2,"答复序号":null,'
            '"客户问题":"询问培训安排","我方答复":""}]')
        self.assertEqual(len(items), 2)
        self.assertEqual([i.kind for i in items], ["报错", "需求"])
        self.assertEqual([i.status for i in items],
                         [QA_STATUS_DONE, QA_STATUS_PENDING])

    def test_模型重复报同一条会去重(self):
        items = self.run_with(
            '[{"类型":"报错","问题序号":0,"答复序号":1,"客户问题":"卡住","我方答复":"修"},'
            '{"类型":"报错","问题序号":0,"答复序号":1,"客户问题":"卡住","我方答复":"修"}]')
        self.assertEqual(len(items), 1)

    def test_解析失败要抛错以便上层降级(self):
        with self.assertRaises(LLMError):
            self.run_with("我拒绝输出JSON")

    def test_无消息内容时返回空且不调用模型(self):
        empty = ChatBundle(contact=Contact(cid="c", name="空", kind="friend"),
                           start=date(2026, 8, 1), end=date(2026, 8, 31), messages=[])
        with mock.patch.object(qa_ai, "_chat") as m:
            self.assertEqual(qa_ai.extract_qa_ai(empty, {}), [])
            m.assert_not_called()

    def test_长会话会分块且不丢中间(self):
        # 造 60 条长消息，强制多块；每块都必须被调用到
        msgs = []
        for i in range(60):
            who = "张三" if i % 2 == 0 else BOT
            msgs.append((5, 9, i % 60, who, f"第{i}条消息，" + "内容" * 200))
        b = mk(msgs)
        calls = []

        def fake(cfg, messages, max_tokens):
            calls.append(messages[-1]["content"])
            return "[]"

        with mock.patch.object(qa_ai, "_chat", side_effect=fake):
            qa_ai.extract_qa_ai(b, {"qa_chunk_chars": 3000})
        self.assertGreater(len(calls), 1, "长会话应当分块")
        joined = "\n".join(calls)
        # 首、中、尾的消息都要出现在某一块里（对照 transcript() 会丢中间）
        for probe in ("第0条消息", "第30条消息", "第59条消息"):
            self.assertIn(probe, joined, probe)


class TestEngineDispatch(unittest.TestCase):
    """pipeline 的引擎调度：AI 不可用时必须降级，且要说清楚原因。"""

    def setUp(self):
        from core import pipeline
        self.pipeline = pipeline
        self.b = mk([
            (5, 9, 12, "张三", "审批到第二级卡住了，说没有匹配的审批人"),
            (5, 9, 20, BOT, "收到，是审批流里海南分组没绑对应审批节点，我今晚修"),
        ])

    def test_ai失败降级到离线并给出原因(self):
        with mock.patch.object(qa_ai, "_chat", side_effect=LLMError("接口返回 401")):
            items, eng, note = self.pipeline.extract_qa_engine(
                self.b, {"engine": "auto", "llm_api_key": "x", "llm_base_url": "http://x"})
        self.assertEqual(eng, "offline")
        self.assertIn("401", note)
        self.assertIn("降级", note)
        self.assertTrue(items)          # 离线引擎仍然产出了内容

    def test_明确指定offline时不碰AI(self):
        with mock.patch.object(qa_ai, "_chat") as m:
            items, eng, note = self.pipeline.extract_qa_engine(self.b, {"engine": "offline"})
            m.assert_not_called()
        self.assertEqual(eng, "offline")
        self.assertEqual(note, "")
        self.assertTrue(items)

    def test_引擎选择不改变输入(self):
        # 两条引擎共用 prepare_entries，输入一致是"差异只来自判定方式"的前提
        off, _, _ = self.pipeline.extract_qa_engine(self.b, {"engine": "offline"})
        with mock.patch.object(qa_ai, "_chat", return_value=(
                '[{"类型":"报错","问题序号":0,"答复序号":1,'
                '"客户问题":"审批卡在第二级","我方答复":"海南分组没绑审批节点"}]')):
            ai, eng, _ = self.pipeline.extract_qa_engine(
                self.b, {"engine": "ai", "llm_api_key": "x", "llm_base_url": "http://x"})
        self.assertEqual(eng, "ai")
        self.assertEqual(len(ai), 1)
        self.assertTrue(off)


if __name__ == "__main__":
    unittest.main(verbosity=2)
