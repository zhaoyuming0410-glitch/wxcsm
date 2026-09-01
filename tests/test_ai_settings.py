"""AI 设置连通性测试：用最小 demo 会话真实调用 summarize_ai，验证大模型能否被调通。

用法：
    python tests/test_ai_settings.py
输出：打印配置来源、请求目标、HTTP 状态码/错误、模型返回正文（或降级原因）。
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

# 让脚本能 import 到 core 包
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import load  # noqa: E402
from core.models import ChatBundle, Contact, Message  # noqa: E402
from core.summarizer import summarize_ai, LLMError  # noqa: E402


def _build_demo_bundle() -> ChatBundle:
    """一段极简、但包含我方/客户双方发言的沟通样本，足以触发总结。"""
    msgs = [
        Message(ts=__import__("datetime").datetime(2026, 8, 20, 10, 1),
                sender="赵禹铭", text="您好，关于 SSO 对接我们这边想本月底完成联调。", is_self=True),
        Message(ts=__import__("datetime").datetime(2026, 8, 20, 10, 5),
                sender="客户", text="好的，我们后端接口这周能给您白名单，字段映射表稍后发您。", is_self=False),
        Message(ts=__import__("datetime").datetime(2026, 8, 21, 14, 30),
                sender="赵禹铭", text="字段映射已收到，配置完成，预计明天生效。", is_self=True),
        Message(ts=__import__("datetime").datetime(2026, 8, 22, 9, 12),
                sender="客户", text="辛苦，另外希望报销导出能支持按利润中心拆分，有没有办法？", is_self=False),
    ]
    return ChatBundle(
        contact=Contact(cid="demo_test", name="示例客户-测试", kind="friend"),
        start=__import__("datetime").date(2026, 8, 20),
        end=__import__("datetime").date(2026, 8, 22),
        messages=msgs,
    )


def main() -> int:
    cfg = load()
    print("=" * 60)
    print("AI 设置连通性测试")
    print("=" * 60)
    print(f"llm_base_url : {cfg.get('llm_base_url')!r}")
    print(f"llm_model    : {cfg.get('llm_model')!r}")
    print(f"llm_api_key  : {'已配置(%d字符)' % len(cfg.get('llm_api_key') or '') if cfg.get('llm_api_key') else '空'}")
    print(f"engine       : {cfg.get('engine')!r}")
    print(f"请求目标 URL : {cfg.get('llm_base_url','').rstrip('/')}/chat/completions")
    print("=" * 60)

    if not cfg.get("llm_api_key"):
        print("✗ 未配置 API Key，无法测试（请到 GUI 填入或用环境变量注入）。")
        return 2

    bundle = _build_demo_bundle()
    print(f"[试跑] 用 {bundle.count} 条消息调用大模型…")
    try:
        text = summarize_ai(bundle, cfg)
        ok = 50 <= len(text.replace(" ", "")) <= 200
        print("✓ 大模型调用成功！")
        print(f"  返回正文（{len(text.replace(' ', ''))} 字，字数达标={ok}）:")
        print("  " + "-" * 40)
        for line in text.splitlines() or [text]:
            print("  " + line)
        print("  " + "-" * 40)
        return 0
    except LLMError as e:
        print("✗ 大模型调用失败（LLMError）:")
        print("  " + str(e))
        traceback.print_exc()
        return 1
    except Exception as e:  # noqa: BLE001
        print("✗ 意料外异常:")
        print("  " + repr(e))
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
