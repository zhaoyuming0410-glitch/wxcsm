"""聊天数据源抽象。

这是整个工具最关键的设计决策：把「聊天记录从哪来」和「怎么总结、怎么导出」彻底解耦。
微信的取数手段会随版本变化而失效，但只要适配器接口不变，上层 GUI / 总结 / 导出一行都不用改。

新增一个数据源只需三件事：继承 ChatSource、实现 list_contacts 与 fetch、在 registry 里注册。
"""

from __future__ import annotations

import hashlib
import shutil
import zipfile
from abc import ABC, abstractmethod
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..models import Contact, Message

# 压缩包自动解压的缓存目录（幂等，按 zip 路径+大小+修改时间生成子目录）
_ZIP_CACHE = Path.home() / ".wxcsm" / "zip_extract"


def resolve_zip_root(path: Path) -> Path:
    """若 path 是 .zip，则解压到幂等缓存目录并返回解压根；否则原样返回。

    - 缓存键 = zip 绝对路径 + 大小 + 修改时间，内容不变直接复用，避免重复解压。
    - 做了 zip-slip 路径穿越防护，解压到缓存目录之外的内容会被拒。
    - 解压失败（损坏/非法路径）抛出 SourceError，给人话级提示。
    """
    if path.suffix.lower() != ".zip":
        return path
    if not path.exists():
        raise SourceError(f"压缩包不存在：{path}")
    st = path.stat()
    digest = hashlib.sha1(
        f"{path.resolve()}::{st.st_size}::{st.st_mtime}".encode("utf-8")
    ).hexdigest()[:16]
    out_dir = _ZIP_CACHE / digest
    if out_dir.exists() and any(out_dir.iterdir()):
        return out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in list(out_dir.glob("*")):           # 清掉可能的半截解压残留
        if old.is_dir():
            shutil.rmtree(old)
        else:
            old.unlink()
    try:
        with zipfile.ZipFile(path) as z:
            base = out_dir.resolve()
            for info in z.infolist():
                dest = (out_dir / info.filename).resolve()
                if base != dest and base not in dest.parents:
                    raise SourceError(f"压缩包含非法路径，已拒解压：{info.filename}")
                if info.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(z.read(info))
    except zipfile.BadZipFile as e:
        raise SourceError(f"压缩包损坏或不是 zip 文件：{path}（{e}）")
    return out_dir


class SourceError(Exception):
    """数据源层面的可预期错误，界面上直接展示给业务人员看。"""


class ChatSource(ABC):
    key: str = "base"
    label: str = "未命名数据源"
    hint: str = ""
    needs_path: bool = False
    path_kind: str = "dir"          # dir | file | url
    path_label: str = "数据路径"
    default_path: str = ""          # url 类数据源的默认地址（GUI 预填用）

    def __init__(self, **options: Any) -> None:
        self.options: Dict[str, Any] = options

    # ---------- 子类必须实现 ----------
    @abstractmethod
    def list_contacts(self) -> List[Contact]:
        """返回全部可选聊天对象。"""

    @abstractmethod
    def fetch(self, contact: Contact, start: date, end: date) -> List[Message]:
        """取出 [start 00:00:00, end 23:59:59] 区间内该对象的全部消息，按时间升序。"""

    # ---------- 可选覆写 ----------
    def health(self) -> Tuple[bool, str]:
        """连通性自检。界面在用户点「加载聊天对象」前先调它，给出人话级提示。"""
        return True, "就绪"

    # ---------- 通用工具 ----------
    @staticmethod
    def day_bounds(start: date, end: date) -> Tuple[datetime, datetime]:
        return (
            datetime.combine(start, time.min),
            datetime.combine(end, time(23, 59, 59)),
        )

    @staticmethod
    def sort_messages(messages: List[Message]) -> List[Message]:
        return sorted(messages, key=lambda m: m.ts)
