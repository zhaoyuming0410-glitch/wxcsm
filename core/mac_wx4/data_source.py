# -*- coding: utf-8 -*-
"""macOS 微信 4.x 直读(wx4)数据源适配器(仅 macOS 生效)。

流程(与 Windows wx4 对齐, 差异在取钥与解密参数):
  1. locate.detect_mac_accounts()  找账号 db_storage;
  2. memkey.scan_wechat_keys()     从运行中微信进程内存扫到 {salt: enc_key};
  3. 逐库: 读 .db 第1页盐 → 从内存密钥表取该库 enc_key → decrypt 到 staging;
  4. reader.scan_contacts / fetch_messages 读联系人/消息。

⚠️ 真实前提(必须如实告知用户):
  - 仅 macOS; 仅支持微信 4.x;
  - 需先一次性 ad-hoc 重签微信去掉 Hardened Runtime(否则 task_for_pid 读不到内存),
    此步要 sudo, 属系统级操作;
  - 微信须在运行且已登录;
  - 整体未真机端到端验证。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ..models import Contact, Message
from ..sources.base import ChatSource, SourceError
from . import decrypt, locate, memkey, reader

STAGING_ROOT = Path.home() / ".wxcsm" / "mac_wx4_staging"


class MacWxLiveSource(ChatSource):
    key = "wx4mac"
    label = "直接读取本机微信（macOS 4.x）聊天记录"
    hint = ("自动定位本机已登录的 macOS 微信 4.x 并读取加密聊天库。\n"
            "首次使用需: ① 已用 sudo 重签微信去掉 Hardened Runtime; "
            "② 微信保持运行且已登录(以便从内存取库密钥)。\n"
            "(本数据源仅 macOS; 若不可用请改用「导入文件 / 已解密库」。)")
    needs_path = False
    path_kind = "无"

    def __init__(self, **options):
        super().__init__(**options)
        self._account: Optional[locate.MacAccount] = None
        self._salt2key: Dict[str, str] = {}
        self._staged: Optional[Path] = None
        self._contacts: List[Contact] = []
        self._scanned = False
        self._progress_cb: Optional[Callable[[str], None]] = options.get("progress_cb")

    def _echo(self, msg: str) -> None:
        if self._progress_cb:
            try:
                self._progress_cb(msg)
            except Exception:
                pass

    # ---------------- 平台门槛 ----------------
    def _assert_platform(self) -> None:
        if sys.platform != "darwin":
            raise SourceError(
                "「直接读取本机微信(macOS)」数据源仅在 macOS 上可用。\n"
                "当前系统不是 macOS, 请用其余数据源(内置样例/导入文件/已解密库)。")

    # ---------------- 账号与密钥 ----------------
    def _resolve_account(self) -> locate.MacAccount:
        if self._account:
            return self._account
        accts = locate.detect_mac_accounts()
        if not accts:
            raise SourceError(
                "没有在本机找到 macOS 微信 4.x 的数据目录(db_storage with message)。\n"
                "请确认: ① Mac 上装了微信 4.x 并登录过; ② 数据在默认容器路径。\n"
                "若探测不到, 请改用「导入聊天记录文件 / 读取已解密数据库」。")
        self._account = accts[0]  # 最近活跃
        self._echo(f"选用账号 {self._account.account}")
        return self._account

    def _obtain_salt2key(self) -> Dict[str, str]:
        if self._salt2key:
            return self._salt2key
        try:
            self._echo("扫描运行中的微信进程内存以取得各库密钥…")
            self._salt2key = memkey.scan_wechat_keys(progress=self._echo)
        except Exception as e:
            raise SourceError(f"从内存取密钥失败: {e}\n"
                              "请确认已 sudo 重签微信去掉 Hardened Runtime、微信运行且已登录。")
        if not self._salt2key:
            raise SourceError("内存中未扫描到微信库密钥。请确认微信已运行且已登录后重试。")
        return self._salt2key

    def _key_for(self, db: Path) -> Optional[str]:
        salt = decrypt.probe_salt(str(db))
        if not salt:
            return None  # 明文或读不了
        return self._salt2key.get(salt.hex().lower())

    # ---------------- 暂存与解密 ----------------
    def _stage_and_decrypt(self) -> Path:
        if self._staged and self._staged.exists():
            return self._staged
        acc = self._resolve_account()
        salt2key = self._obtain_salt2key()

        # 目标库集合
        src_dbs: List[Path] = list(acc.message_dbs)
        if acc.contact_db:
            src_dbs.append(acc.contact_db)
        if acc.session_db:
            src_dbs.append(acc.session_db)
        if not src_dbs:
            raise SourceError("该账号下没有找到 message/contact 库文件。")

        out_dir = STAGING_ROOT / (acc.account or "account")
        out_dir.mkdir(parents=True, exist_ok=True)

        missing_keys: List[str] = []
        done = 0
        for src in src_dbs:
            try:
                rel = src.relative_to(acc.db_storage)
            except ValueError:
                rel = Path(src.name)
            dst = out_dir / rel
            if dst.exists() and reader._is_decrypted(dst):
                done += 1
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            # 明文库直接拷贝
            if src.read_bytes()[:16] == b"SQLite format 3\x00":
                dst.write_bytes(src.read_bytes())
                done += 1
                continue
            key = self._key_for(src)
            if not key:
                missing_keys.append(src.name)
                continue
            tmp = dst.with_suffix(".db.tmp")
            try:
                decrypt.decrypt_db_file(str(src), key, str(tmp), check_hmac=True)
                tmp.replace(dst)
                done += 1
                self._echo(f"已解密 {rel}")
            except decrypt.MacDecryptError:
                missing_keys.append(src.name)
        self._staged = out_dir
        if missing_keys:
            self._echo(f"以下库未能匹配密钥/解密(跳过): {', '.join(missing_keys[:6])}")
        if done == 0:
            raise SourceError(
                "没有任何库被解密成功。常见原因: ① 微信未在运行(内存无密钥); "
                "② 未先 sudo 重签微信去掉 Hardened Runtime; ③ 版本非 4.x 或库结构变化。")
        return out_dir

    # ---------------- 健康检查 ----------------
    def health(self) -> Tuple[bool, str]:
        try:
            self._assert_platform()
        except SourceError as e:
            return False, str(e)
        try:
            acc = self._resolve_account()
        except SourceError as e:
            return False, str(e)
        if not memkey.find_wechat_pids():
            return False, (
                f"已找到账号 {acc.account}, 但微信未在运行。请打开并登录 macOS 微信 4.x, "
                "并确认已 sudo 重签去掉 Hardened Runtime 后再试。")
        return True, f"已识别账号 {acc.account}({len(acc.message_dbs)} 个消息库); 微信运行中"

    # ---------------- 联系人 / 消息 ----------------
    def list_contacts(self) -> List[Contact]:
        self._assert_platform()
        if self._scanned:
            return self._contacts
        root = self._stage_and_decrypt()
        self._contacts = reader.scan_contacts(root)
        self._scanned = True
        return self._contacts

    def fetch(self, contact: Contact, start: date, end: date) -> List[Message]:
        self._assert_platform()
        root = self._stage_and_decrypt()
        acc = self._resolve_account()
        try:
            return reader.fetch_messages(root, contact, start, end,
                                         account_hint=acc.account)
        except RuntimeError as e:
            raise SourceError(str(e))
