"""数据源注册表。GUI 的下拉框直接由此生成，新增数据源无需改界面代码。"""

from __future__ import annotations

from typing import Dict, List, Type

from .base import ChatSource, SourceError
from .demo import DemoSource
from .importer import ImportSource
from .sqlite_db import DecryptedDbSource

REGISTRY: Dict[str, Type[ChatSource]] = {
    cls.key: cls for cls in (DemoSource, ImportSource, DecryptedDbSource)
}

ORDER: List[str] = ["demo", "import", "wxdb"]


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
