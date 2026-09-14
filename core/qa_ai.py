# -*- coding: utf-8 -*-
"""客户问题答复清单 —— AI 引擎。

与离线引擎（`qa_extract.extract_qa`）的关系：
    两条引擎共用 `qa_extract.prepare_entries()` 的清洗结果，输入完全一致，
    差异只来自"怎么判"。AI 跑不通（没配密钥/网络异常/JSON 解析失败）时由
    pipeline 自动降级到离线，保证这个功能不会因为大模型不可用而整块失效。

核心设计：**让模型引用消息序号，而不是自己写时间**
    聊天记录每行渲染成 `#序号 MM-DD HH:MM 发送人：内容`，模型只回答
    「问题序号」「答复序号」。时间、原始记录一律用序号回填真实 Message。
    这样模型没有机会编造时间戳，也不会因为 MM-DD 格式写歪而整条丢失；
    序号无效的条目可以直接丢掉，而不是污染表格。

    相比之下，让模型直接输出"提问时间"字符串的做法既难校验、又难对齐
    「原始记录」列——那一列存在的意义就是能核对原话，必须是真原文。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import (
    QA_KINDS, QA_STATUS_DONE, QA_STATUS_PENDING, QA_STATUS_PROMISED, ChatBundle, Message,
    QaItem,
)
from .qa_extract import iter_chunks, prepare_entries
from .summarizer import LLMError

# 每条消息在 prompt 里的渲染上限：太长的消息（整段 XML 残留、长表格粘贴）
# 会把预算吃光，反而挤掉后面的真问题。
_LINE_CHARS = 300

SYSTEM_PROMPT = """你是客户沟通记录整理助手。下面给你的是「{name}」在 {time_range} 的聊天记录。

每行格式：`#序号 MM-DD HH:MM 发送人：内容`
发送人显示为「我」或我方同事名字的是**我方**，其余是**客户方**。

请把这段记录里的【客户提出的问题】和【我方主动承诺的待办】逐条整理出来。

类型判定口径：
- 提问：客户在问事实、问怎么做、问时间节点
- 需求：客户希望我们做某件事（含「麻烦…」「请帮我…」「要求…」）
- 报错：客户反馈功能不正常、出错、卡住、走不通
- 投诉：客户表达不满、抱怨、质疑
- 我方待办：客户没提问，但我方主动认领了要做的事（「我拉一份表给你」「我今天配好」）

输出要求：
1. 「问题序号」填客户那条消息的 #号；「答复序号」填我方对应答复的 #号，没有答复就填 null。
2. **只能引用上面真实出现过的 #号，绝不编造。**
3. 「客户问题」「我方答复」用简洁书面语重述原意，不要改变意思；分别控制在 80 字、150 字内。
4. 「状态」：已答复＝答复序号有效；未答复（待跟进）＝客户问了但我方在这段记录里没回；
   已承诺（待跟进）＝我方待办。
   **没答复时「我方答复」必须留空、答复序号填 null**，不要写「未直接答复」
   「客户表示了解」这类描述——那是状态，不是答复内容。
5. 「嗯」「好的」「收到」这类纯应答不要单独成条。
6. 「我方待办」只用于**不对应任何客户提问**的我方主动承诺（客户没问，我方说
   「我拉一份表给你」「我今天配好」）。如果这句承诺本身就是在回答客户的问题，
   就并进那条的「我方答复」，不要再单独出一条。
7. 同一条消息里说了几件事，合并成一条，不要把一条拆成多条。
8. 如果这段记录里没有值得整理的内容，输出空数组 []。

只输出 JSON 数组，不要解释、不要 markdown 代码块。格式：
[{{"类型":"提问","问题序号":12,"答复序号":15,"客户问题":"…","我方答复":"…","状态":"已答复"}}]"""

USER_PROMPT = """聊天对象：{name}
时间范围：{time_range}
消息条数：{count}

聊天记录：
{transcript}"""


# ---------------------------------------------------------------------
#  模型调用
# ---------------------------------------------------------------------
def _chat(cfg: Dict[str, Any], messages: List[Dict[str, str]], max_tokens: int) -> str:
    """调用 OpenAI 兼容接口，返回正文。

    与 `summarizer.summarize_ai` 的请求构造保持一致（同一个 base_url 拼接、
    同样的 thinking 开关），但这里不做字数重试——问答清单的正确性不能靠
    "再写一遍"来救，解析失败就该降级到离线。
    """
    import requests  # 延迟导入：离线模式下不强制依赖

    api_key = (cfg.get("llm_api_key") or "").strip()
    if not api_key:
        raise LLMError("未配置 API Key")
    base = (cfg.get("llm_base_url") or "").strip().rstrip("/")
    if not base:
        raise LLMError("未配置接口地址")
    url = base + ("" if base.endswith("/chat/completions") else "/chat/completions")

    payload: Dict[str, Any] = {
        "model": cfg.get("llm_model") or "kimi-k2-turbo-preview",
        "messages": messages,
        "temperature": float(cfg.get("qa_temperature", 0.2)),
        "max_tokens": max_tokens,
    }
    thinking = (cfg.get("llm_thinking") or "").strip().lower()
    if thinking in ("disabled", "enabled"):
        payload["thinking"] = {"type": thinking}

    try:
        resp = requests.post(
            url, json=payload,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            timeout=int(cfg.get("llm_timeout", 90)),
        )
    except Exception as e:  # noqa: BLE001 —— 统一转成 LLMError 交给上层降级
        raise LLMError(f"网络请求失败：{e}") from e
    if resp.status_code != 200:
        raise LLMError(f"接口返回 {resp.status_code}：{resp.text[:180]}")
    try:
        return resp.json()["choices"][0]["message"]["content"] or ""
    except Exception as e:  # noqa: BLE001
        raise LLMError(f"响应格式异常：{e}") from e


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)


def _parse_json_array(text: str) -> List[Dict[str, Any]]:
    """从模型输出里抠出 JSON 数组。

    实测模型会不听话：加 ```json 代码块、前后写一句"好的，以下是结果"、
    最后一项多一个逗号。这些都按"尽力解析"处理，真解不出才抛错降级。
    """
    s = (text or "").strip()
    if not s:
        raise LLMError("模型返回为空")
    s = _FENCE.sub("", s).strip()
    # 去掉代码块围栏（可能出现多段，逐个剥）
    s = s.replace("```json", "").replace("```", "").strip()
    lo, hi = s.find("["), s.rfind("]")
    if lo == -1 or hi == -1 or hi < lo:
        raise LLMError(f"模型未返回 JSON 数组：{s[:120]}")
    body = s[lo:hi + 1]
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        # 容错：去掉对象/数组末尾多余逗号后重试
        fixed = re.sub(r",\s*([\]}])", r"\1", body)
        try:
            data = json.loads(fixed)
        except json.JSONDecodeError as e:
            raise LLMError(f"JSON 解析失败：{e}；原文 {body[:120]}") from e
    if not isinstance(data, list):
        raise LLMError("模型返回的不是数组")
    return [x for x in data if isinstance(x, dict)]


def _as_int(v: Any) -> Optional[int]:
    """把模型给的序号转成 int；'12' / '#12' / 12.0 都要能用，其余返回 None。"""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    s = str(v).strip().lstrip("#").strip()
    return int(s) if s.isdigit() else None


# ---------------------------------------------------------------------
#  渲染与抽取
# ---------------------------------------------------------------------
def _render(chunk: Sequence[Tuple[int, Message, str]]) -> str:
    lines = []
    for idx, m, clean in chunk:
        who = "我" if m.is_self else (m.sender or "对方")
        body = clean if len(clean) <= _LINE_CHARS else clean[:_LINE_CHARS] + "…"
        lines.append(f"#{idx} {m.ts:%m-%d %H:%M} {who}：{body}")
    return "\n".join(lines)


def _clip(s: str, limit: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= limit else s[:limit].rstrip() + "…"


# 模型有时不做"留空"，而是把"我方没答复"这件事写成答复正文
# （实测出现过「（未直接答复）」「（未直接答复，客户表示了解）」）。
# 不处理的话表格里会出现「已答复：未直接答复」这种自相矛盾的行，
# 而且会把它算进"已答复"指标，掩盖真实的服务缺口。
#
# 另一半是**状态词本身**：模型偶尔把「已答复」「已处理」当答复正文写回来。
# 这比"未直接答复"更糟——它让一条没答的问题看起来答复过了，却零信息量。
# 实测真实数据里出现过 10 行答复正文就是「已答复。」（根因是我方原话被 XML
# 清洗吃掉只剩 "57"，模型无内容可写。清洗已修，但这道闸必须留着）。
_META_ANSWER = re.compile(
    r"未(直接)?(答复|回应|回复|作答|给出)|无(明确)?答复|没有(直接)?(答复|回复)"
    r"|客户表示(了解|知晓|收到)|未提及|暂无"
    r"|^已(答复|回复|回应|处理|解决|跟进|沟通|说明|告知)(了|过)?$")


def _is_meta_answer(s: str) -> bool:
    """判断这条"答复"其实只是在描述"没有答复"。"""
    t = (s or "").strip().strip("（）()【】[]。，,；; ")
    return bool(t) and len(t) <= 24 and bool(_META_ANSWER.search(t))


def _build_item(rec: Dict[str, Any], bundle: ChatBundle, by_idx: Dict[int, Tuple[Message, str]],
                ask_limit: int, ans_limit: int) -> Optional[QaItem]:
    """把模型的一条记录落成 QaItem；序号无效就丢掉（宁缺勿滥）。"""
    kind = str(rec.get("类型") or "").strip()
    if kind not in QA_KINDS:
        kind = "提问"
    q_idx = _as_int(rec.get("问题序号"))
    a_idx = _as_int(rec.get("答复序号"))

    # 我方待办：客户没提问，位置由我方那条消息定
    anchor_idx = a_idx if kind == "我方待办" else q_idx
    if anchor_idx is None or anchor_idx not in by_idx:
        return None                      # 拿不到真实消息就没法回填时间与原始记录
    a_msg, a_clean = by_idx.get(a_idx, (None, "")) if a_idx is not None else (None, "")
    q_msg, q_clean = by_idx.get(q_idx, (None, "")) if q_idx is not None else (None, "")
    anchor_msg = by_idx[anchor_idx][0]

    question = _clip(str(rec.get("客户问题") or ""), ask_limit)
    answer = _clip(str(rec.get("我方答复") or ""), ans_limit)
    # 客户问题列不能是空的（除非是我方待办），否则这一行没有信息量
    if kind != "我方待办" and not question:
        return None
    # 「（未直接答复）」这类描述不是答复，按没答复处理
    if answer and _is_meta_answer(answer):
        answer = ""
        a_msg = None

    # 答复时间只在真的配上了答复时才填。我方待办行不填——它还没被答复，
    # 填上时间会让人误以为已闭环（离线引擎也是这么处理的，两条引擎必须一致）。
    ans_ts: Optional[datetime] = None
    if kind == "我方待办":
        status = QA_STATUS_PROMISED
        question, q_msg = "", None
    elif a_msg is not None and answer:
        status = QA_STATUS_DONE
        ans_ts = a_msg.ts
    else:
        status = QA_STATUS_PENDING
        answer = ""
        a_msg = None

    # 原始记录一律用真实原话拼，不用模型的重述——这一列就是用来核对原话的
    raw_parts = []
    if q_msg is not None and q_clean:
        raw_parts.append(f"客户[{q_msg.ts:%m-%d %H:%M}]：{q_clean}")
    if a_msg is not None and a_clean:
        raw_parts.append(f"我方[{a_msg.ts:%m-%d %H:%M}]：{a_clean}")
    if not raw_parts and kind == "我方待办":
        raw_parts.append(f"我方[{anchor_msg.ts:%m-%d %H:%M}]：{by_idx[anchor_idx][1]}")

    order = anchor_idx * 100 + (99 if kind == "我方待办" else 0)
    return QaItem(
        customer_name=bundle.contact.name,
        kind=kind,
        question=question,
        answer=answer,
        status=status,
        raw=" ‖ ".join(raw_parts),
        ask_ts=(q_msg.ts if kind != "我方待办" and q_msg is not None else anchor_msg.ts),
        answer_ts=ans_ts,
        order=order,
    )


def extract_qa_ai(bundle: ChatBundle, cfg: Optional[Dict[str, Any]] = None) -> List[QaItem]:
    """用大模型从 ChatBundle 抽出问答清单。

    抛 LLMError 表示 AI 这条路走不通（上层会降级到离线）；正常返回可能是空列表
    （确实没有值得整理的内容），两者语义不同，不要混为一谈。
    """
    cfg = cfg or {}
    ask_limit = int(cfg.get("qa_ask_chars", 80))
    ans_limit = int(cfg.get("qa_answer_chars", 150))
    chunk_chars = int(cfg.get("qa_chunk_chars", 12000))
    max_tokens = int(cfg.get("qa_max_tokens", 4096))

    entries = prepare_entries(bundle)
    if not entries:
        return []
    by_idx = {idx: (m, clean) for idx, m, clean in entries}

    chunks = list(iter_chunks(entries, chunk_chars))
    sys_msg = SYSTEM_PROMPT.format(name=bundle.contact.name, time_range=bundle.time_range)

    records: List[Tuple[int, Dict[str, Any]]] = []
    for ci, chunk in enumerate(chunks):
        user_msg = USER_PROMPT.format(
            name=bundle.contact.name, time_range=bundle.time_range, count=len(chunk),
            transcript=_render(chunk),
        )
        # 末块常被截断，补一句提示，避免模型以为记录不完整而硬凑答复
        if ci == len(chunks) - 1 and len(chunks) > 1:
            user_msg += "\n\n（以上是整段记录的最后一部分。）"
        text = _chat(cfg, [{"role": "system", "content": sys_msg},
                           {"role": "user", "content": user_msg}], max_tokens)
        for rec in _parse_json_array(text):
            records.append((ci, rec))

    # 去重：同一 (类型, 问题序号, 答复序号) 只留一条。分块之间不重叠，
    # 重复只可能来自模型自己把一条拆成两条。
    seen, items = set(), []
    for _ci, rec in records:
        key = (str(rec.get("类型") or ""), _as_int(rec.get("问题序号")),
               _as_int(rec.get("答复序号")))
        if key in seen:
            continue
        seen.add(key)
        it = _build_item(rec, bundle, by_idx, ask_limit, ans_limit)
        if it is not None:
            items.append(it)

    items.sort(key=lambda x: x.order)
    return items
