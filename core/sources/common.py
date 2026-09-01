"""第三方微信提取工具（WeFlow 等）共用的消息解析 helpers。

两条原则：
  1) 不读微信进程内存、不解密；本文件只做「已解密的 JSON → 内部 Message 模型」的转换。
  2) 微信 Msg.type 的数字编码（1 文本 / 3 图片 / 34 语音 / 43 视频 / 49 分享 …）
     在 WeFlow 等第三方工具里一致，所以媒体占位表、类型归类可以共用，
     避免两个适配器各写一份、久而久之对不齐。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

# 文本类消息类型（content 字段就是可读文字，可直接喂给总结引擎）
TEXT_TYPES = {1, 10000}
# 媒体 / 特殊类型 → 用简短占位，绝不把原始 XML 倒给总结引擎
MEDIA_LABEL = {
    3: "[图片]", 34: "[语音]", 43: "[视频]", 47: "[动画表情]", 42: "[名片]",
    48: "[位置]", 50: "[语音通话]", 62: "[拍一拍]", 87: "[群公告]",
    2000: "[转账]", 2001: "[红包]", 2003: "[红包封面]",
}
# type=49 分享类的子类型 → 占位前缀
SHARE_SUB = {5: "[链接]", 6: "[文件]", 8: "[GIF表情]", 19: "[合并转发]",
             33: "[小程序]", 36: "[小程序]", 51: "[视频号]", 57: "[引用]",
             63: "[视频号]", 92: "[音乐]"}


def parse_time(raw: Any) -> Optional[datetime]:
    """把各类时间表示解析成 datetime，解析失败返回 None。

    兼容：RFC3339（含 Z 后缀）、纯秒级时间戳、纯毫秒级时间戳、多种本地格式。
    WeFlow 的 createTime 为秒级 Unix 时间戳（parse_time 也兼容 RFC3339 等格式，便于对接其它工具）。
    """
    if not raw:
        return None
    s = str(raw).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # 纯数字 → 时间戳（10 位秒 / 13 位毫秒）
    if s.isdigit():
        try:
            n = int(s)
            if n > 10_000_000_000:  # 毫秒级
                n = n / 1000
            return datetime.fromtimestamp(n)
        except Exception:
            return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
               "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def type_label(mtype: int) -> str:
    """把微信 Msg.type 数字归到内部 msg_type 词表（text/image/voice/video/share/card/system/other）。"""
    if mtype == 10000:
        return "system"
    if mtype in TEXT_TYPES:
        return "text"
    if mtype == 3:
        return "image"
    if mtype == 34:
        return "voice"
    if mtype == 43:
        return "video"
    if mtype == 49 or mtype in SHARE_SUB:
        return "share"
    if mtype == 42:
        return "card"
    return "other"


def media_placeholder(mtype: int) -> str:
    """媒体 / 非文本类型的简短占位；type=49 统一给 [分享]（更细的子类由调用方决定）。"""
    if mtype == 49:
        return "[分享]"
    return MEDIA_LABEL.get(mtype, "[消息]")
