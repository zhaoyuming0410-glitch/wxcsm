"""沟通总结生成器。

双引擎设计（刻意如此）：
  ai      —— 调用大模型（OpenAI 兼容接口，默认月之暗面 Kimi），质量高，需网络与 API Key
  offline —— 纯本地抽取式摘要，零依赖零成本，断网 / 没配 Key 也能出结果

为什么要保留离线引擎？因为业务工具最怕的不是效果差一点，而是"今天用不了"。
auto 模式：能用 AI 就用 AI，任一环节出问题自动降级到离线，并在结果里标注引擎来源。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from .models import ChatBundle, Summary

SYSTEM_PROMPT = """你是企业费用管理 SaaS 公司的资深客户成功经理助理。你的任务是阅读一段与客户的微信沟通记录，输出一篇供客户成功团队归档复盘用的沟通总结。

硬性要求：
1. 字数严格控制在 {min_chars}-{max_chars} 个字之间（不含标点空格也应落在此区间）。
2. 在一段连贯叙述中覆盖以下要点（记录中有则写，没有则跳过，绝对不要编造）：
   - 核心沟通事项：这段时间主要在推进什么
   - 客户侧的需求、疑问、抱怨或障碍
   - 我方已完成的对接/配置与后续动作（卡点、已承诺的下一步及时间）
3. 输出形式为一段连贯的中文陈述（总结体），并严格遵守以下禁忌：
   - 不要分点、不要小标题、不要 Markdown 符号、不要开场客套；
   - 禁止出现「反馈问题：」「诉求：」「我方对接进度：」这类固定标签词；
   - 禁止用「客户说…」「我们回复…」等引述对话的句式——只做客观归纳，不照搬谁说了什么；
   - 用客观第三人称，「客户」指对方，「我方」指本公司。
4. 只依据记录内容陈述，不推测、不美化、不添加记录里没有的信息。
5. 直接输出总结正文，不要任何前缀说明。
6. 总结正文中不要出现客户名称（如「与XX沟通」「XX反馈」），也不要出现时间范围（如「本期」「X月X日」「近一周」等）；直接陈述核心内容，客户名称与时间范围已作为独立字段另行归档。"""

USER_PROMPT = """客户名称：{name}
沟通时间范围：{time_range}
消息条数：{count}

沟通记录如下：
---
{transcript}
---

请输出 {min_chars}-{max_chars} 字的沟通总结。"""

# ---------- 离线引擎词典 ----------
TOPIC_LEXICON = {
    "费控标准": ("费控", "标准", "分档", "额度", "超标", "管控"),
    "报销流程": ("报销", "发票", "查验", "核销", "对账", "关账", "凭证"),
    "差旅管理": ("差旅", "出差", "机票", "酒店", "住宿", "餐补", "行程", "打车", "用车"),
    "审批流配置": ("审批", "审批流", "节点", "审批人", "提单", "单据"),
    "系统对接": ("对接", "SSO", "接口", "API", "同步", "联调", "白名单", "字段", "映射"),
    "组织与人员": ("组织", "人员", "导入", "员工", "部门", "架构", "实名", "账号", "权限"),
    "预算管理": ("预算", "预算中心", "成本中心", "利润中心", "卡控"),
    "对公支付": ("对公", "付款", "支付", "打款", "结算", "充值"),
    "报表与数据": ("报表", "数据", "导出", "统计", "看板", "台账"),
    "培训与赋能": ("培训", "手册", "讲解", "演示", "宣导", "通知"),
    "续费与商务": ("续费", "合同", "到期", "报价", "优惠", "预算收紧", "政策", "签"),
    "实施上线": ("实施", "上线", "启动会", "里程碑", "试运行", "验证", "测试"),
}

# ---------- 客户生命周期阶段（仅用于总结侧重，绝不写进正文） ----------
# 离线引擎默认从对话关键词推断阶段；也可在配置 customer_stage 显式指定以覆盖推断。
# 不同阶段的"段落优先级"不同：续费期把风险(问题)抬到最高，确保续约风险不被裁掉；
# 其余阶段以"已做进度"为先，体现推进势头。
STAGE_HINTS = {
    "续费期": ("续费", "到期", "合同", "报价", "优惠", "续约", "renewal", "商务", "签"),
    "实施中": ("实施", "上线", "启动会", "里程碑", "试运行", "验证", "联调", "配置", "对接"),
    "新签客户": ("开通", "注册", "试用", "激活", "入驻", "初始化", "首"),
}
STAGE_PRIORITY = {
    "续费期":   {"progress": 2, "issue": 1, "demand": 3},
    "实施中":   {"progress": 1, "issue": 2, "demand": 3},
    "新签客户": {"progress": 1, "issue": 2, "demand": 3},
    "稳定使用": {"progress": 1, "issue": 2, "demand": 3},
}


def _infer_stage(text: str) -> str:
    """按关键词重叠推断客户生命周期阶段；判不出归'稳定使用'。"""
    best, best_score = "稳定使用", 0
    for stage, words in STAGE_HINTS.items():
        score = sum(text.count(w) for w in words)
        if score > best_score:
            best, best_score = stage, score
    return best

DEMAND_HINTS = ("希望", "想", "能不能", "能否", "可以吗", "需要", "要求", "建议", "麻烦",
                "有没有办法", "怎么", "如何", "什么时候", "诉求", "期望", "最好")
ISSUE_HINTS = ("报错", "错误", "失败", "卡住", "不行", "异常", "找不到", "很慢",
               "问题", "投诉", "不太顺", "用不了", "空的", "没有匹配", "推不动", "出错",
               "被驳回", "对不上", "差了", "超标", "不够", "不匹配")
PROGRESS_HINTS = ("已", "完成", "配好", "配置完成", "上线", "生效", "通过", "解决", "发您",
                  "发你", "提交", "确认", "安排", "预计", "下周", "明天", "今天下午",
                  "工作日", "同步给", "拉您", "跟进", "待", "计划")
NOISE = re.compile(r"^(好的|收到|谢谢|感谢|嗯+|ok|OK|在吗|你好|早上好|哈哈+|[。，！？.!?]*)$")
SENT_SPLIT = re.compile(r"[。！？；\n]+")


def _clip(text: str, limit: int) -> str:
    """按句边界裁剪，避免出现半句话。"""
    text = re.sub(r"\s+", "", text)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in ("。", "；", "，"):
        idx = cut.rfind(sep)
        if idx >= int(limit * 0.6):
            return cut[: idx + 1]
    return cut


_LEAD_PERSON = re.compile(
    r"^(我们这边|我们|咱们|你们|你方|我方|我这边|我司|咱)[，：:，。、]?"
)


def _depersonalize(text: str) -> str:
    """去掉第一/二人称主体标记，让客户原话变成客观陈述。

    例如『我们这边 SSO 对接想本月底完成』→『SSO 对接想本月底完成』，
    避免总结正文出现『我方/客户』说话的句式。
    """
    return _LEAD_PERSON.sub("", text).strip()


# 第二人称（您/你/你们/你方）——带这类词的小句几乎都是"回复客户"的对话句，必须丢弃
_SECOND = re.compile(r"[您你你们你方]")
# 口语填充词：出现在小句开头时整句偏闲扯，去掉后留下的才是事实
_FILLER_LEAD = re.compile(r"^(另外|还有|以及|同时|然后|其实|那个|这个)")
_FILLER_TRAIL = re.compile(r"(也想一起上|也想|也一起上|一起上|呀|啊|呢|吧|哦|噻|哈)$")
# 纯客套/应答句（兜底抽取时用来跳过，不进总结）
_PLEASANTRY = re.compile(r"^(嗯+|哦+|好的|好|行|那|额|哎|哈哈+|OK|ok)")


def _clean_fact(text: str, strip_hints: bool = False) -> str:
    """把一条抽取到的原话洗成客观事实短语。

    处理链：去主语(我方/我们) → 按逗号拆小句 → 丢弃含第二人称的小句 →
    去掉口语填充词与诉求类引导词 → 去句尾标点 → 用顿号并接。
    strip_hints=True 时，每个小句开头的诉求引导词（希望/想/需要/能不能…）都会被去掉，
    让需求句更像结论而非"客户说希望…"的对话答复。
    """
    _HINT_LEAD = re.compile(r"^(希望|想|需要|要求|建议|期望|最好|麻烦|能否|能不能|可以吗|有没有办法)")
    text = _depersonalize(text)
    out = []
    for c in re.split(r"[，,；;]", text):
        c = c.strip()
        if not c or len(c) < 4 or _SECOND.search(c) or _PLEASANTRY.match(c):
            continue
        if strip_hints:
            c = _HINT_LEAD.sub("", c)
        c = _FILLER_LEAD.sub("", c)
        c = _FILLER_TRAIL.sub("", c)
        c = c.strip("，。；、 ")
        if c:
            out.append(c)
    return "，".join(out)


def enforce_length(text: str, min_chars: int, max_chars: int) -> Tuple[str, bool]:
    """返回 (规整后的文本, 是否满足字数区间)。"""
    text = re.sub(r"^[\s\-•*#>]+", "", (text or "").strip())
    text = re.sub(r"\*\*|__|`|#{1,6}\s*", "", text)
    text = re.sub(r"[ \t]+", "", text).replace("\n", "")
    core = len(re.sub(r"\s", "", text))
    if core > max_chars:
        text = _clip(text, max_chars)
        if not text.endswith(("。", "！", "？")):
            text += "。"
        core = len(re.sub(r"\s", "", text))
    return text, min_chars <= core <= max_chars


# =====================================================================
#  离线抽取式引擎
# =====================================================================
def summarize_offline(bundle: ChatBundle, min_chars: int = 50, max_chars: int = 200,
                     stage: Optional[str] = None) -> str:
    msgs = [m for m in bundle.messages if m.text and not NOISE.match(m.text.strip())]
    if not msgs:
        return ("无有效沟通记录，未产生可归档的客户诉求或对接进展，"
                "建议主动触达确认客户当前状态与使用情况。")

    joined = "。".join(m.text for m in msgs)

    # 客户阶段：显式指定优先，否则从对话关键词推断（仅决定段落优先级侧重，不写进正文）
    stage = stage or _infer_stage(joined)
    stage_priority = STAGE_PRIORITY.get(stage, STAGE_PRIORITY["稳定使用"])

    # 1) 主题命中：按关键词出现次数排序，取前三
    hits: List[Tuple[str, int]] = []
    for topic, words in TOPIC_LEXICON.items():
        score = sum(joined.count(w) for w in words)
        if score:
            hits.append((topic, score))
    hits.sort(key=lambda x: -x[1])
    topics = "、".join(t for t, _ in hits[:3]) or "日常使用与服务对接"
    topic_list = [t for t, _ in hits[:3]]

    # 2) 客户诉求 / 问题：只看客户侧发言，抽取后立即洗成客观事实短语
    cust = [m.text for m in msgs if not m.is_self]
    demands, issues = [], []
    for text in cust:
        for sent in SENT_SPLIT.split(text):
            s = sent.strip()
            if len(s) < 5 or NOISE.match(s):
                continue
            if any(h in s for h in DEMAND_HINTS) and len(demands) < 3:
                fact = _clean_fact(_clip(s, 46), strip_hints=True)
                if fact and fact not in demands:
                    demands.append(fact)
            elif any(h in s for h in ISSUE_HINTS) and len(issues) < 3:
                fact = _clean_fact(_clip(s, 46))
                if fact and fact not in issues:
                    issues.append(fact)

    # 3) 对接进度：看我方发言里的承诺与完成项，优先取最近的
    mine = [m.text for m in msgs if m.is_self]
    progress: List[str] = []
    for text in reversed(mine):
        for sent in SENT_SPLIT.split(text):
            s = sent.strip()
            if len(s) < 6 or NOISE.match(s):
                continue
            if any(h in s for h in PROGRESS_HINTS):
                fact = _clean_fact(_clip(s, 50))
                if fact:
                    progress.append(fact)
                break
        if len(progress) >= 2:
            break
    # 兜底：关键词没命中时，取我方最后一句"非客套、非第二人称"的有效发言。
    # 依据是——我方最后说的话几乎总代表当前进度或已承诺的下一步，
    # 但纯应答句（"嗯，有需要再找我"）对客户成功归档没有价值，必须跳过。
    if not progress:
        for text in reversed(mine):
            cands = [s.strip() for s in SENT_SPLIT.split(text)
                     if len(s.strip()) >= 6 and not NOISE.match(s.strip())
                     and not _SECOND.search(s.strip()) and not _PLEASANTRY.match(s.strip())]
            if cands:
                fact = _clean_fact(_clip(cands[-1], 50))
                if fact:
                    progress.append(fact)
                break
    progress.reverse()

    # ---- 按主题合并成句的连贯总结 ----
    # 设计取舍：早期版本把抽取到的句子碎片按「反馈问题 / 诉求 / 我方对接进度」标签分行罗列，
    # 读起来像"谁说了什么"的对话答复；上一版去掉了标签、改成"围绕主题开展工作，事实A、B、C"
    # 的平铺，但仍是一串顿号并列，句子之间缺乏衔接。
    # 现进一步：把每个事实归到它所属的业务主题，同一主题下的"已做 / 问题 / 需求"合并成一句，
    # 用「{主题}方面，…；…。」的句式串联，形成真正成段的叙述，而不是清单。
    def _topic_of(fact: str) -> str:
        """按关键词重叠判断一条事实属于哪个主题；判不出归'其他事项'。"""
        best, best_score = "", 0
        for topic, words in TOPIC_LEXICON.items():
            score = sum(fact.count(w) for w in words)
            if score > best_score:
                best, best_score = topic, score
        return best

    # 归集：topic -> {'progress':[], 'issue':[], 'demand':[]}，并记录首次出现顺序
    buckets: Dict[str, Dict[str, List[str]]] = {}
    seen_order: List[str] = []
    for fact, kind in ([(f, "progress") for f in progress]
                       + [(f, "issue") for f in issues]
                       + [(f, "demand") for f in demands]):
        t = _topic_of(fact) or "其他事项"
        if t not in buckets:
            buckets[t] = {"progress": [], "issue": [], "demand": []}
            seen_order.append(t)
        buckets[t][kind].append(fact)

    # 主题顺序：先按热力排序的 topic_list，再补归集中多出来的主题
    ordered_topics = [t for t in topic_list if t in buckets] + [t for t in seen_order if t not in topic_list]

    # 每个主题的句子由若干"段"组成，每段带优先级：进度=1（最高）> 问题=2 > 需求=3（最先砍）。
    # 这样超长裁剪时先丢需求、再丢问题，永远保住"已做进度"这一段对客户成功归档最关键的复盘信息。
    topic_segs: Dict[str, List[Tuple[int, str]]] = {}
    for t in ordered_topics:
        b = buckets[t]
        segs: List[Tuple[int, str]] = []
        if b["progress"]:
            segs.append((stage_priority["progress"], "，".join(b["progress"])))
        if b["issue"]:
            segs.append((stage_priority["issue"], f"存在{'，'.join(b['issue'])}等情况，需进一步排查处理"))
        if b["demand"]:
            segs.append((stage_priority["demand"], f"客户提出{'，'.join(b['demand'])}等需求，待排期落实"))
        topic_segs[t] = segs

    def _assemble(max_priority: int) -> str:
        """只保留优先级 <= max_priority 的段（数字越小越重要）。"""
        parts = []
        for t in ordered_topics:
            kept = [s for (p, s) in topic_segs[t] if p <= max_priority]
            if not kept:
                continue
            body = "；".join(kept)
            parts.append(f"{t}方面，{body}。" if t != "其他事项" else f"{body}。")
        return "".join(parts)

    if not any(topic_segs.values()):
        text = "沟通以信息同步为主，暂无明确未闭环事项，建议持续关注客户使用状态。"
    else:
        # 进度优先：默认全保留；超长则依次丢弃需求(3)、问题(2)，最后只剩进度(1)
        text = _assemble(3)
        if len(re.sub(r"\s", "", text)) > max_chars:
            text = _assemble(2)
        if len(re.sub(r"\s", "", text)) > max_chars:
            text = _assemble(1)
    text, ok = enforce_length(text, min_chars, max_chars)

    if not ok and len(re.sub(r"\s", "", text)) < min_chars:
        # 字数不足：补一句结构化收尾，而不是硬凑字
        tail = (f"建议后续持续跟进上述事项，确认客户使用状态并推动未闭环需求按期落地。")
        text, _ = enforce_length(text.rstrip("。") + "。" + tail, min_chars, max_chars)
    else:
        # 字数已达标，但因优先级裁剪丢掉了部分段落：补一句指向，避免信息无声丢失。
        # 只提实际被砍的段（需求优先级最低最先砍，问题次之），不凭空捏造。
        dropped = []
        if demands and "客户提出" not in text:
            dropped.append("需求")
        if issues and "需进一步排查" not in text:
            dropped.append("问题")
        if dropped:
            label = "、".join(dropped)
            if len(re.sub(r"\s", "", text)) <= max_chars - (len(label) + 10):
                text = text.rstrip("。") + f"。其余{label}详见下方明细。"
    return text


# =====================================================================
#  AI 引擎
# =====================================================================
class LLMError(Exception):
    pass


def summarize_ai(bundle: ChatBundle, cfg: Dict[str, Any]) -> str:
    import requests  # 延迟导入：离线模式下不强制依赖

    api_key = (cfg.get("llm_api_key") or "").strip()
    if not api_key:
        raise LLMError("未配置 API Key")
    base = (cfg.get("llm_base_url") or "").strip().rstrip("/")
    if not base:
        raise LLMError("未配置接口地址")
    url = base + ("" if base.endswith("/chat/completions") else "/chat/completions")
    min_chars = int(cfg.get("min_chars", 50))
    max_chars = int(cfg.get("max_chars", 200))

    sys_msg = SYSTEM_PROMPT.format(min_chars=min_chars, max_chars=max_chars)
    user_msg = USER_PROMPT.format(
        name=bundle.contact.name, time_range=bundle.time_range, count=bundle.count,
        transcript=bundle.transcript(), min_chars=min_chars, max_chars=max_chars,
    )
    messages = [{"role": "system", "content": sys_msg},
                {"role": "user", "content": user_msg}]

    last_text = ""
    for attempt in range(2):
        payload = {
            "model": cfg.get("llm_model") or "kimi-k2-turbo-preview",
            "messages": messages,
            # 不同模型对 temperature 约束不同（如 kimi-k2.7-code 只接受 1），故改为可配置；默认 0.3
            "temperature": float(cfg.get("llm_temperature", 0.3)),
            "max_tokens": int(cfg.get("llm_max_tokens", 2048)),
        }
        # 推理类模型(hy3 等)的思考开关：短总结场景关掉可大幅降延迟与消耗，且避免思考占满预算导致正文为空
        _thinking = (cfg.get("llm_thinking") or "").strip().lower()
        if _thinking in ("disabled", "enabled"):
            payload["thinking"] = {"type": _thinking}
        try:
            resp = requests.post(
                url, json=payload,
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                timeout=int(cfg.get("llm_timeout", 60)),
            )
        except Exception as e:
            raise LLMError(f"网络请求失败：{e}") from e

        if resp.status_code != 200:
            detail = resp.text[:180].replace("\n", " ")
            raise LLMError(f"接口返回 {resp.status_code}：{detail}")
        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            raise LLMError(f"响应格式异常：{e}") from e

        text, ok = enforce_length(content, min_chars, max_chars)
        last_text = text
        if ok:
            return text
        # 字数没达标，把实际字数回灌给模型再来一次
        actual = len(re.sub(r"\s", "", text))
        messages += [
            {"role": "assistant", "content": text},
            {"role": "user", "content":
                f"上一版实际 {actual} 字，不符合 {min_chars}-{max_chars} 字要求。"
                f"请重写，保持要点完整、语言通顺，只输出总结正文。"},
        ]
    return last_text


# =====================================================================
#  统一入口
# =====================================================================
def summarize(bundle: ChatBundle, cfg: Dict[str, Any]) -> Summary:
    min_chars = int(cfg.get("min_chars", 50))
    max_chars = int(cfg.get("max_chars", 200))
    engine = (cfg.get("engine") or "auto").lower()

    if bundle.count == 0:
        return Summary(
            customer_name=bundle.contact.name, time_range=bundle.time_range,
            content=summarize_offline(bundle, min_chars, max_chars, cfg.get("customer_stage") or None),
            msg_count=0, engine="offline", error="该时间范围内无消息",
        )

    err = ""
    if engine in ("auto", "ai"):
        try:
            text = summarize_ai(bundle, cfg)
            return Summary(customer_name=bundle.contact.name, time_range=bundle.time_range,
                           content=text, msg_count=bundle.count, engine="ai")
        except Exception as e:
            err = str(e)
            if engine == "ai":
                return Summary(
                    customer_name=bundle.contact.name, time_range=bundle.time_range,
                    content=summarize_offline(bundle, min_chars, max_chars, cfg.get("customer_stage") or None),
                    msg_count=bundle.count, engine="offline",
                    error=f"AI 生成失败已自动降级：{err}",
                )

    return Summary(
        customer_name=bundle.contact.name, time_range=bundle.time_range,
        content=summarize_offline(bundle, min_chars, max_chars, cfg.get("customer_stage") or None),
        msg_count=bundle.count, engine="offline",
        error=(f"AI 不可用已自动降级：{err}" if err else ""),
    )
