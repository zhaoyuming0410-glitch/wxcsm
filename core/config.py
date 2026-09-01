"""配置读写。存放在用户目录，避免工具目录被设为只读时写不进去。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

CONFIG_DIR = Path.home() / ".wxcsm"
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULTS: Dict[str, Any] = {
    "source": "demo",
    "source_path": "",
    "self_names": [],                                 # 代表「我方」的发送人名：导入源（真实姓名无我方标记时）+ 解密数据库源（校正群聊归错人、统一显示名字）共用
    "engine": "auto",                                  # auto | ai | offline
    "llm_base_url": "https://api.moonshot.cn/v1",
    "llm_model": "kimi-k2-turbo-preview",
    "llm_api_key": "",
    "llm_timeout": 60,
    "llm_max_tokens": 2048,                       # 推理类模型(hy3 等)思考会占预算，需留足；非推理模型到自然停止即止，设大无害
    "llm_thinking": "",                           # 仅推理类模型(hy3 等)用：""=不传 / "disabled"=关思考(短总结推荐) / "enabled"=开深度思考
    "llm_temperature": 0.3,                       # 采样温度；部分模型有硬约束（kimi-k2.7-code 只接受 1），故可配置
    "llm_provider": "",                           # 模型来源标记：""=手动填写 / "workbuddy"=取自本机 WorkBuddy 模型
    "llm_wb_model": "",                           # 当 llm_provider=workbuddy 时，记录所选 WorkBuddy 模型的 id（用于重新打开时预选）
    "min_chars": 50,
    "max_chars": 200,
    "customer_stage": "",                              # 客户生命周期阶段：空=自动推断（续费期/实施中/新签客户/稳定使用）
    "export_path": str(Path.home() / "Desktop" / "微信客户沟通总结.xlsx"),
    "export_mode": "append",                           # append | overwrite
}

# 环境变量优先级高于配置文件，便于 WorkBuddy 自动化注入密钥而不落盘
ENV_MAP = {
    "llm_api_key": ("WXCSM_API_KEY", "MOONSHOT_API_KEY", "KIMI_API_KEY", "OPENAI_API_KEY"),
    "llm_base_url": ("WXCSM_BASE_URL", "OPENAI_BASE_URL"),
    "llm_model": ("WXCSM_MODEL",),
}


def load() -> Dict[str, Any]:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass  # 配置损坏不应让工具起不来，静默回落默认值
    for key, env_names in ENV_MAP.items():
        for name in env_names:
            val = os.environ.get(name)
            if val:
                cfg[key] = val
                break
    return cfg


def save(cfg: Dict[str, Any]) -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    keep = {k: v for k, v in cfg.items() if k in DEFAULTS}
    CONFIG_PATH.write_text(
        json.dumps(keep, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return CONFIG_PATH
