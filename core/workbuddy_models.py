"""读取本机 WorkBuddy 已配置的模型，供 GUI 的「AI 设置」直接选用。

WorkBuddy（底座为腾讯 CodeBuddy）的模型配置存放在用户目录的 models.json 里，
每项含 url / apiKey / 模型名。wxcsm 的 AI 引擎走 OpenAI 兼容接口，
因此这里只需把 WorkBuddy 的模型条目「翻译」成引擎需要的 llm_* 配置即可，
真正做到「生成总结时调用 WorkBuddy 的模型」，无需硬编码任何地址。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

# 候选配置文件：优先 .workbuddy（本机实际位置），兼容 .codebuddy
_CANDIDATES = [
    Path.home() / ".workbuddy" / "models.json",
    Path.home() / ".codebuddy" / "models.json",
]


def _raw_models() -> List[Dict]:
    """从候选路径里读出原始模型列表（可能是 {"models":[...]} 或直接 [...] 两种形态）。"""
    for p in _CANDIDATES:
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        models = data.get("models") if isinstance(data, dict) else data
        if isinstance(models, list) and models:
            return models
    return []


def scan_workbuddy_models() -> List[Dict[str, str]]:
    """返回 WorkBuddy 已配置模型列表，每项字段：
    id / name / vendor / url / apiKey / supportsReasoning / useCustomProtocol。
    找不到文件或解析失败时返回空列表（调用方据此禁用该来源）。
    """
    out: List[Dict[str, str]] = []
    for m in _raw_models():
        if not isinstance(m, dict):
            continue
        url = (m.get("url") or "").strip()
        if not url:
            continue
        mid = m.get("id") or m.get("name") or ""
        out.append({
            "id": mid,
            "name": m.get("name") or mid or "",
            "vendor": m.get("vendor") or "",
            "url": url,
            "apiKey": m.get("apiKey") or "",
            "supportsReasoning": bool(m.get("supportsReasoning", False)),
            "useCustomProtocol": bool(m.get("useCustomProtocol", False)),
        })
    return out


def resolve_for_engine(model: Dict[str, str]) -> Dict[str, str]:
    """把 WorkBuddy 模型条目转成 summarize_ai 需要的 llm_* 配置（并规范化 url 路径）。

    - 标准协议（useCustomProtocol=false）：保证 url 以 /chat/completions 结尾，
      否则补上（summarize_ai 也只在缺这个后缀时才补，这里提前规整更稳）。
    - 自定义协议：用户填的就是完整地址，原样返回。
    - 温度：推理类模型（kimi-k2.7-code / kimi-k3 等）多数要求 temperature=1，
      非推理模型用较稳的 0.3。
    - 思考：默认不传（留空）。原因——WorkBuddy 里的推理模型（kimi 系）拒绝
      thinking=disabled（只接受 enabled 或不传），故不再像 hy3 那样默认关思考；
      需要深度思考的用户可在 GUI 的「思考深度」里显式选「开启」。
    """
    url = (model.get("url") or "").strip().rstrip("/")
    use_custom = model.get("useCustomProtocol", False)
    if use_custom or url.endswith("/chat/completions"):
        base_url = url
    else:
        base_url = url + "/chat/completions"
    is_reason = bool(model.get("supportsReasoning", False))
    temperature = 1.0 if is_reason else 0.3
    return {
        "llm_base_url": base_url,
        "llm_model": model.get("id") or model.get("name") or "",
        "llm_api_key": model.get("apiKey") or "",
        "llm_thinking": "",
        "llm_temperature": temperature,
    }


def display_label(model: Dict[str, str]) -> str:
    """下拉框里展示用的文字：模型名（供应商）。"""
    name = model.get("name") or model.get("id") or "未命名模型"
    vendor = model.get("vendor") or ""
    return f"{name}（{vendor}）" if vendor else name
