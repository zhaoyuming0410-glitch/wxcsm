"""数据源注册表。GUI 的下拉框直接由此生成，新增数据源无需改界面代码。"""

from __future__ import annotations

import sys
from typing import Dict, List, Type

from .base import ChatSource, SourceError
from .demo import DemoSource
from .importer import ImportSource
from .sqlite_db import DecryptedDbSource
from .wx4_live import Wx4LiveSource

# macOS 版「直读本机微信」适配器：跨平台可安全导入，但仅在 macOS 上向用户展示/可选。
# (其内部依赖 Mach VM + Hardened Runtime 重签，Windows 上不可用，故不进入下拉框。)
try:
    from .mac_wx4 import MacWxLiveSource
except Exception:  # pragma: no cover — 即便导入异常也不影响其它数据源
    MacWxLiveSource = None

IS_MAC = sys.platform == "darwin"

# 排序: Windows/通用在前; macOS 专属 wx4mac 放最后, 且仅 mac 时加入可选
_BASE_KEYS = ["wx4", "demo", "import", "wxdb"]
_MAC_KEY = "wx4mac"

_REG = [DemoSource, ImportSource, DecryptedDbSource, Wx4LiveSource]
if IS_MAC and MacWxLiveSource is not None:
    _REG.append(MacWxLiveSource)

REGISTRY: Dict[str, Type[ChatSource]] = {cls.key: cls for cls in _REG}

ORDER: List[str] = list(_BASE_KEYS)
if IS_MAC and "wx4mac" in REGISTRY:
    ORDER.append("wx4mac")


def create(key: str, **options) -> ChatSource:
    cls = REGISTRY.get(key)
    if cls is None:
        raise SourceError(f"未知数据源：{key}（可选：{', '.join(ORDER)}）")
    return cls(**options)


def choices() -> List[tuple]:
    """[(key, label, hint, needs_path, path_label), ...]"""
    return [
        (k, REGISTRY[k].label, REGISTRY[k].hint,
         REGISTRY[k].needs_path, REGISTRY[k].path_label)
        for k in ORDER if k in REGISTRY
    ]


__all__ = ["ChatSource", "SourceError", "REGISTRY", "ORDER", "create", "choices"]
