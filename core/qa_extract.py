# -*- coding: utf-8 -*-
"""客户问题答复清单抽取（离线引擎）。

目标：把「一个聊天对象 + 一段时间」里的沟通，整理成一张
「客户提出了什么 → 我方怎么答复/处理」的表格，每个问题一行。

与 core/summarizer.py 的关系：
  summarizer 负责「叙述式总结」——把一段时间压成一段话，允许丢细节，只要主线对。
  本模块负责「问答清单」——不允许丢问题，因为漏掉的问题在表里是"无声消失"，
  用户根本不知道漏了。这是两者最根本的差别，也决定了实现方式不同：
  summarizer 走"拼 transcript 喂模型"；本模块**直接逐条遍历消息**，不经任何截断。

判定范围（用户 2026-09 确认，取最宽）：
  提问（疑问句/问号）、需求（希望/麻烦/帮我…）、报错、投诉，
  外加「我方主动承诺的待办」——这类没有客户提问，单独用 kind='我方待办' 区分，
  避免和客户问题混在一起分不清。

设计取舍：
  - 表格里的「客户问题 / 我方答复」保留原话口径（只做噪音清洗+截断），
    刻意**不套用 summarizer._clean_fact**。因为 _clean_fact 会把句子洗成客观短语碎片
    （"我们这边 SSO 对接想本月底完成" → "SSO 对接想本月底完成"），那适合写进叙述总结，
    但读表格的人是想知道"客户原话问了什么"，洗碎了反而看不懂。
    完整原话一律进「原始记录」列，随时可核对。
  - 本模块**不复用 summarizer 的 _DEMAND_IMP/_ISSUE_OKAY 等判定正则**。那套正则是为
    "写一段叙述总结"调的，宽容度换召回率，直接搬来判问答会出三类错（均已实测踩到）：
      · `请(帮|协助|处理)?` 允许裸「请」→「申请**单**」里的"请"被当成请求动词；
      · 故障词「不能」会命中「能**不能**给个优惠」的子串 → 正常提问被判成报错；
      · 愿望词里的裸「想」会命中「比我想的问题多」→ 评论被判成需求。
    所以下面另起一套精度优先的线索。
"""
from __future__ import annotations

import re
from datetime import timedelta
from difflib import SequenceMatcher
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from .models import (
    QA_STATUS_DONE, QA_STATUS_PENDING, QA_STATUS_PROMISED, ChatBundle, Message, QaItem,
)
from .summarizer import NOISE, SENT_SPLIT, _PLEASANTRY, _strip_message_noise

# 非纯文本消息没有可读内容，问答清单直接跳过（其 text 是 XML/占位噪音）
_MEDIA_TYPES = {"图片", "视频", "语音", "表情", "名片", "位置",
                "文件/链接", "系统消息", "image", "video", "voice", "file"}

# ---------- 分类线索（精度优先） ----------

# 疑问形式：问号，或疑问词
_Q_MARK = re.compile(r"[？?]")
_Q_WORD = re.compile(
    r"(怎么|如何|为什么|为何|能不能|能否|是否|有没有|有无|什么时候|多久|多少|"
    r"哪里|哪个|哪一种|哪一|可不可以|可以吗|行不行|对不对|对吗|是不是|"
    r"还是|什么情况|咋办|咋弄)"
)

# 「能不能 / 能否 / 可不可以」是能力询问，本身不是故障。
# **必须在扫故障词之前摘掉**，否则「能不能给个优惠」里的子串「不能」会让整句被判成报错。
_CAPABILITY = re.compile(r"(能不能|能否|可不可以|可以吗|行不行|是不是)")

# 「在引出提问」的元话语。命中说明这句是在引述/引出提问，而不是在报故障——
# 不加这道闸，「我们财务提了个问题，城市级别能不能按我们自己的标准分档」会因为
# 含"问题"二字被判成报错，把一次正常咨询标成故障。
_META_ASK = re.compile(
    r"(提了个问题|提个问题|问个问题|有个问题|提了问题|咨询一下|咨询下|"
    r"请教|想了解|想咨询|想请教|问题如下|问一下|问下)"
)

# 报错/故障：只用明确的故障词。
# 刻意**不收「问题」「出错」这两个高频泛词**——「我们这块出错最多」含"出错"、
# 「比我想的问题多」含"问题"，都不是故障描述；拿它们当线索会把正常评论误判成报错。
# 「有问题/出问题」则保留：它是真的在报故障（"没问题"不含"有问题"，不会误伤）。
_BUG = ("有问题", "出问题", "存在问题", "报错", "错误", "失败", "卡住", "异常",
        "找不到", "很慢", "用不了", "打不开", "显示空", "空的", "没有匹配",
        "被驳回", "对不上", "超标", "不匹配", "无法", "不能", "定不了",
        "没有对应", "不可用", "不完整", "没通过", "无票", "没结束",
        "申请不了", "上传不了", "调不了", "不生效", "没生效")

# 投诉/不满：体验与情绪层面，和"功能坏了"区分开。
# 顺序上先于报错判定——「今年用得不太顺」同时含"不太顺"，若先判报错会被标成故障，
# 实际它是体验抱怨而非缺陷。
_COMPLAINT = ("投诉", "不满", "太麻烦", "太复杂", "太慢", "难用", "不好用",
              "用得不太顺", "不顺畅", "受不了", "推不动", "体验差", "很差",
              "老是出错", "总是出错", "抱怨", "不满意", "失望")

# 请求：**不认光秃秃的「请」**。summarizer._DEMAND_IMP 用 `请(帮|协助|处理)?`，
# 会把「申请单」里的"请"当请求动词（实测：整句仅因"申请单"就被判成需求）。
# QA 判定要求精确，所以这里一律要求「请」后面跟具体动作。
_REQUEST = re.compile(
    r"(帮我|帮忙|帮我方|帮我们|协助|麻烦|烦请|"
    r"请(?:帮|协助|处理|把|给|安排|出|发|看|查|确认|支持|同步|配合)|"
    r"能不能帮|能否帮|"
    r"能安排|安排吗|安排一下|安排个|配一下|配个|配一个|出一份|出个|"
    r"发我|发一份|给我|给我们|拉个|拉一份|协调一下|支持一下|同步一下|"
    r"给个|给一版|给份|拆一?[下个条])"
)

# 命令式请求：「把 X 讲/配/加…一点」这类没有"请/帮我"的祈使句
_REQ_CMD = re.compile(r"把[^，。；？！]{0,20}?(?:做|讲|配|调|加|减|开|关|改|处理|发|给|看|查|拆)")

# 表愿望的词。**bare「想」不能收**——「比我想的问题多」会中招，要求"想"后跟动作或趋向量。
_WANT = re.compile(
    r"(希望|想要|需要|要求|期望|建议|诉求|最好是|盼|"
    r"想(?:要|让|请|把|用|做|上|跑|聊|了解|在|尽|先|跟|和|给|看看|看到))"
)

# 客户陈述约束/底线：隐含需求。不收会漏掉
# 「部门要和我们费控预算中心对上，不然预算卡不住」这种真实诉求。
_CONSTRAINT = re.compile(r"(必须|务必|一定要|千万|不能少|得先|不然|否则|要保证|需要保证)")

# 我方"往前看"的承诺（会变成待办）。要求第一人称动作或明确的服务承诺——
# 刻意**不收光秃秃的「下周/明天」**：那会把我方的进度通报、第三方日程也算成待办，
# 表格里会冒出一堆并不是我们认领的事项。
_COMMIT = re.compile(
    r"(我[^，。；？！]{0,5}(?:来|会|去|安排|协调|同步|拉|出|给|发|记|提|帮|配|修|申请|确认|跟进)|"
    r"帮您|给您|发您|拉一份|拉个|出一份|预计[^，。；]{0,8}(?:生效|完成|上线|好)|"
    r"下一步我|稍后|我这边|我先)"
)
# 已完成的表述：命中则不当作"待办"（避免把做完的事列成待办）
_DONE_MARK = re.compile(r"(已完成|已配置完成|已补齐|已生效|已发|已解决|已上线|已通过|已处理)")

_WS = re.compile(r"\s+")
# 句尾标点（判断"吗/呢"结尾的疑问句前先剥掉）
_TAIL_PUNCT = re.compile(r"[。．.!！?？~～、,，;；:：]+$")
_PUNCT_ALL = re.compile(r"[\s。．.!！?？~～、,，;；:：\"'“”‘’（）()\[\]【】\-—_]+")

# 清洗后仍像 XML/app 载荷的内容：这类消息没有可读业务文字，混进表格就是垃圾行。
# 真实数据里出现过 "10000000000000001:57</aeskey"、"10000000000000001:57<" 这类残渣被
# 当成"我方答复"。末尾的裸 "<" 也要算信号——被截断的标签只剩一个尖括号，很常见。
_XML_RESIDUE = re.compile(r"<\?xml|<!\[CDATA\[|\]\]>|</\w*>?|aeskey|<[a-z_]+>|<\s*$", re.I)
# 裸 id/密钥残渣，形如 "10000000000000001:57"。要求冒号前是 6 位以上连续 ASCII 标识、
# 冒号后**没有任何中文**，这样「赵老师:好的」「时间:10点」这类正常短句不会被误杀
# （中文名/中文词一般不足 6 个 ASCII 字符，且冒号后必然有中文）。
_BARE_ID = re.compile(r"^[\w.@-]{6,}[:：][^\u4e00-\u9fff]*$")


def _looks_like_payload(s: str) -> bool:
    """判断这条消息清洗后是否仍是无业务含义的技术载荷。"""
    if not s:
        return True
    return bool(_XML_RESIDUE.search(s)) or bool(_BARE_ID.match(s))

# 同一条消息里命中多类时，按这个优先级取一个标签（数字小的赢）
_KIND_PRIORITY = {"报错": 0, "投诉": 1, "需求": 2, "提问": 3}


def _norm(text: str) -> str:
    """归一化用于查重：去空白与所有标点。"""
    return _PUNCT_ALL.sub("", text or "")


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _clip(text: str, limit: int) -> str:
    """截断到 limit 字，尽量在标点处断，避免半句话。"""
    text = _WS.sub("", text or "")
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in ("。", "？", "！", "；", "，", "、"):
        idx = cut.rfind(sep)
        if idx >= int(limit * 0.55):
            return cut[: idx + 1]
    return cut + "…"


def _is_pleasantry(s: str) -> bool:
    """纯客套/应答句判噪音——**只在整句很短时**才这么判。

    不能直接拿 summarizer._PLEASANTRY 当闸门：它是 `^(嗯|哦|好的|好|行|那|额|哎|…)`，
    以「那」开头就算命中，于是「那能不能帮我们看一下配置？」这种正经提问会被误杀。
    """
    return len(s) <= 8 and bool(_PLEASANTRY.match(s))


def classify(text: str) -> Optional[str]:
    """判定一句话属于哪类问题线索；返回 None 表示不是问题（纯客套/已闭环/无信息）。"""
    s = (text or "").strip()
    if len(s) < 5:
        return None
    if NOISE.match(s) or _is_pleasantry(s):
        return None

    meta = bool(_META_ASK.search(s))       # 在引出提问，不是在报故障
    if not meta:
        # 先摘掉能力询问短语，避免 "能不能" 的子串 "不能" 触发故障判定
        scan = _CAPABILITY.sub("", s)
        if any(w in scan for w in _COMPLAINT):
            return "投诉"
        if any(w in scan for w in _BUG):
            return "报错"

    body = _TAIL_PUNCT.sub("", s)
    is_q = bool(_Q_MARK.search(s)) or bool(_Q_WORD.search(s)) or body.endswith(("吗", "呢"))
    has_req = (bool(_REQUEST.search(s)) or bool(_REQ_CMD.search(s))
               or bool(_CONSTRAINT.search(s)))
    has_want = bool(_WANT.search(s))

    if has_req or has_want:
        return "需求"
    if is_q:
        return "提问"
    return None


def _iter_sentences(text: str) -> Iterator[str]:
    for raw in SENT_SPLIT.split(text or ""):
        s = raw.strip()
        if s:
            yield s


def _is_usable_reply(text: str) -> bool:
    """我方回复是否值得当作"答复"：不是纯客套、不是"嗯/收到"，且有实质内容。"""
    s = (text or "").strip()
    if len(s) < 4:
        return False
    return not (NOISE.match(s) or _is_pleasantry(s))


def prepare_entries(bundle: ChatBundle) -> List[Tuple[int, Message, str]]:
    """预处理：清洗噪音，丢掉媒体/空消息/技术载荷。

    返回 (消息在原 bundle 里的下标, Message, 清洗后文本)。保留原下标是为了回填
    时间与顺序，也让 AI 引擎能把模型给出的时间戳映射回真实消息、拼出「原始记录」。
    两条引擎（离线/AI）共用本函数，保证看到的输入完全一致，差异只来自判定方式。
    """
    entries: List[Tuple[int, Message, str]] = []
    for idx, m in enumerate(bundle.messages):
        if (m.msg_type or "text") in _MEDIA_TYPES:
            continue
        clean = _strip_message_noise(m.text or "")
        # 载荷过滤按"清洗后的文本长什么样"判断，而不是按 msg_type：
        # 各数据源对未知类型的约定不一致（wx4 用 typeNNN、已解密库兜底成 text），
        # 靠消息类型筛会漏。
        if not clean or _looks_like_payload(clean):
            continue
        entries.append((idx, m, clean))
    return entries


def iter_chunks(entries: Sequence[Tuple[int, Message, str]], max_chars: int = 12000
                ) -> Iterator[List[Tuple[int, Message, str]]]:
    """把消息按字符预算切块，供 AI 引擎分块调用。

    离线引擎逐条遍历、不需要它；但 AI 引擎必须用——现有 ChatBundle.transcript()
    超长时是「保开头+结尾、丢中间」，对问答清单是致命的：被丢掉的中间部分里的问题
    会无声消失。分块可以保证每条消息都被模型看到过。
    """
    buf: List[Tuple[int, Message, str]] = []
    used = 0
    for e in entries:
        ln = len(e[2]) + 16
        if buf and used + ln > max_chars:
            yield buf
            buf, used = [], 0
        buf.append(e)
        used += ln
    if buf:
        yield buf


def extract_qa(bundle: ChatBundle, cfg: Optional[Dict[str, Any]] = None) -> List[QaItem]:
    """从一个 ChatBundle 抽出问答清单（离线规则引擎）。

    返回按时间先后排序的 QaItem 列表。
    """
    cfg = cfg or {}
    name = bundle.contact.name
    ask_limit = int(cfg.get("qa_ask_chars", 80))
    ans_limit = int(cfg.get("qa_answer_chars", 150))
    gap_days = int(cfg.get("qa_answer_gap_days", 7))
    want_todo = bool(cfg.get("qa_include_our_todo", True))

    # 1) 预处理：清洗噪音，丢掉媒体/空消息/技术载荷。保留消息序号以便回填时间与顺序。
    entries = prepare_entries(bundle)
    if not entries:
        return []

    # 2) 逐句识别客户侧问题（只看客户发言，我方发言是"答复"的来源）。
    #    同一条消息里识别出的多个问题**合并成一行**——客户一口气说的是同一件事，
    #    拆成多行会出现"两个问题各配了一遍同样的答复"的重复感。
    found: List[Tuple[int, str, str]] = []      # (消息下标, 句子, 类别)
    for pos, (_idx, m, clean) in enumerate(entries):
        if m.is_self:
            continue
        sents, kinds = [], []
        for sent in _iter_sentences(clean):
            kind = classify(sent)
            if kind:
                sents.append(sent)
                kinds.append(kind)
        if sents:
            kind = min(kinds, key=lambda k: _KIND_PRIORITY.get(k, 9))
            found.append((pos, "；".join(sents), kind))

    # 3) 去重：同一问题被客户反复追问时只保留最早的条目，
    #    否则表格会被"又想了一下""再问一次"刷屏。
    kept: List[Tuple[int, str, str]] = []
    for item in found:
        if not any(_sim(item[1], k[1]) >= 0.88 for k in kept):
            kept.append(item)

    items: List[QaItem] = []
    used_reply_pos: set = set()

    # 4) 配答复：从提问之后往前找第一条我方有效发言；遇到"下一个客户问题"就停，
    #    避免把后面某条无关的我方消息错配给较早的问题。
    next_q_pos: Dict[int, int] = {}
    for n, (pos, _s, _k) in enumerate(kept):
        if n + 1 < len(kept):
            next_q_pos[pos] = kept[n + 1][0]

    for pos, sent, kind in kept:
        _idx, m, clean = entries[pos]
        stop_at = next_q_pos.get(pos, len(entries))
        answer, answer_ts, answer_pos = "", None, None
        for q in range(pos + 1, min(stop_at, len(entries))):
            _qi, qm, qclean = entries[q]
            if not qm.is_self or not _is_usable_reply(qclean):
                continue
            if qm.ts - m.ts > timedelta(days=gap_days):
                break
            answer, answer_ts, answer_pos = qclean, qm.ts, q
            break

        status = QA_STATUS_DONE if answer else QA_STATUS_PENDING
        raw = f"客户[{m.ts:%m-%d %H:%M}]：{clean}"
        if answer:
            raw += f"  ‖  我方[{answer_ts:%m-%d %H:%M}]：{answer}"
            used_reply_pos.add(answer_pos)

        items.append(QaItem(
            customer_name=name, kind=kind,
            question=_clip(sent, ask_limit),
            answer=_clip(answer, ans_limit),
            status=status, raw=raw,
            ask_ts=m.ts, answer_ts=answer_ts, order=pos * 100,
        ))

    # 5) 我方主动承诺的待办：客户没提问、但我方认领了要做的事。
    #    只收"往前看"的承诺，且排除已作为某条答复用过的消息，避免一句话出现两次。
    if want_todo:
        for pos, (_idx, m, clean) in enumerate(entries):
            if not m.is_self or pos in used_reply_pos:
                continue
            if not _COMMIT.search(clean) or _DONE_MARK.search(clean):
                continue
            if not _is_usable_reply(clean):
                continue
            if any(_sim(clean, it.answer) >= 0.9 for it in items):
                continue
            items.append(QaItem(
                customer_name=name, kind="我方待办",
                question="",
                answer=_clip(clean, ans_limit),
                status=QA_STATUS_PROMISED, raw=f"我方[{m.ts:%m-%d %H:%M}]：{clean}",
                ask_ts=m.ts, answer_ts=None, order=pos * 100 + 99,
            ))

    items.sort(key=lambda it: it.order)
    return items


# 类别展示顺序：客户提的在前，我方待办压到最后
_KIND_ORDER = {"报错": 0, "投诉": 1, "提问": 2, "需求": 3, "我方待办": 4}


def summarize_kinds(items) -> str:
    """给界面/CLI 用的一句话统计，如「报错 2 · 提问 5 · 我方待办 3」。"""
    counts: Dict[str, int] = {}
    for it in items:
        counts[it.kind] = counts.get(it.kind, 0) + 1
    parts = [f"{k} {counts[k]}" for k in sorted(counts, key=lambda x: _KIND_ORDER.get(x, 9))]
    return " · ".join(parts)
