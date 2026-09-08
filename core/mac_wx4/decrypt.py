# -*- coding: utf-8 -*-
"""macOS 微信 4.x 加密库解密器(独立实现)。

⚠️ 如实状态: 本模块按公开资料(macOS 微信 4.x / WCDB / SQLCipher4)独立实现,
   未在真实 macOS 微信上验证, 需在装有微信 4.x 的 Mac 上真机确认。

与 Windows 4.x 的关键差异:
  - Windows 取钥后还要把 raw_key 经 PBKDF2(256000) 派生页面密钥;
  - macOS 进程内存里缓存的 enc_key 直接就是该库的 AES-256 密钥,
    解密时直接 AES-256-CBC, 无需 256000 次 KDF(仅 HMAC 密钥做 2 次 PBKDF2)。

每库独立密钥+盐: 每个 .db 的第 1 页开头 16 字节是盐(salt), 末尾 80 字节
保留区(16B IV + 64B HMAC-SHA512)。enc_key 由内存扫描得到(见 memkey.py),
需按 salt 与本库匹配。

参考(macOS 4.x, 社区项目 wechat-export-macos 的 decrypt_db.py 语义):
  mac_key = PBKDF2-HMAC-SHA512(enc_key, salt ^ 0x3a, 2, 32)
  page_i: iv   = page[4096-80 : 4096-80+16]
          hmac = page[4096-80+16 : 4096]
  AES-256-CBC decrypt( enc_key, iv, page[offset : 4096-80] )   # offset 首页=16, 其余=0
  第 1 页 HMAC 校验作为密钥是否匹配的依据。
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import sqlite3
from pathlib import Path
from typing import Callable, Optional, Tuple

try:  # 优先 PyCryptodome; 无则回退到 cryptography; 再无则抛明确错误
    from Crypto.Cipher import AES
    _AES_AVAILABLE = True
except Exception:  # pragma: no cover
    _AES_AVAILABLE = False

SQLITE_MAGIC = b"SQLite format 3\x00"
PAGE_SIZE = 4096
RESERVE = 80
IV_SIZE = 16
HMAC_SIZE = 64
MAC_ITER = 2

ProgressFn = Callable[[int, int], None]


class MacDecryptError(Exception):
    """macOS 库解密失败(密钥错/文件坏/缺 AES 依赖)。"""


def _require_aes() -> None:
    if _AES_AVAILABLE:
        return
    # 回退: cryptography 库(若装了) ; 否则提示
    try:
        import cryptography  # noqa: F401
        return
    except Exception:
        raise MacDecryptError(
            "需要 PyCryptodome(crypto) 或 cryptography 才能解密 macOS 库。\n"
            "请执行: python3 -m pip install pycryptodome")


def derive_mac_key(enc_key: bytes, salt: bytes) -> bytes:
    """HMAC-SHA512 密钥 = PBKDF2-HMAC-SHA512(enc_key, salt^0x3a, 2, 32)。"""
    mac_salt = bytes(b ^ 0x3A for b in salt)
    return hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, MAC_ITER, dklen=32)


def _page_hmac(mac_key: bytes, data_with_iv: bytes, page_no: int) -> bytes:
    m = hmac_mod.new(mac_key, digestmod=hashlib.sha512)
    m.update(data_with_iv)
    # 页码(小端 4 字节)参与 HMAC —— 与社区实现一致
    m.update(page_no.to_bytes(4, "little"))
    return m.digest()


def _decrypt_block(key: bytes, iv: bytes, payload: bytes) -> bytes:
    if _AES_AVAILABLE:
        return AES.new(key, AES.MODE_CBC, iv).decrypt(payload)
    from cryptography.hazmat.primitives.ciphers import (  # pragma: no cover
        Cipher as _C, algorithms as _A, modes as _M)
    return _C(_A.AES(key), _M.CBC(iv)).decryptor().update(payload)


def probe_salt(db_path: str) -> Optional[bytes]:
    """读取 .db 第 1 页开头 16 字节盐; 若已是明文则返回 None。"""
    try:
        data = Path(db_path).read_bytes()[:PAGE_SIZE]
    except OSError:
        return None
    if len(data) < PAGE_SIZE:
        return None
    if data[:16] == SQLITE_MAGIC:
        return None  # 明文库, 无需解密
    return data[:16]


def decrypt_db_file(db_path: str, enc_key_hex: str, dst: str,
                    check_hmac: bool = True,
                    progress: Optional[ProgressFn] = None) -> dict:
    """用某库对应的 enc_key(内存得到的直接 AES 密钥)把加密库解为明文。

    返回 {status, ok_pages, failed_pages, error?}。
    """
    _require_aes()
    enc_key = bytes.fromhex(enc_key_hex.strip())
    if len(enc_key) != 32:
        raise MacDecryptError("enc_key 必须是 32 字节(64 位 hex)")

    data = Path(db_path).read_bytes()
    if len(data) < PAGE_SIZE:
        raise MacDecryptError(f"文件太小, 不是有效的微信加密库: {db_path}")
    if data[:16] == SQLITE_MAGIC:
        Path(dst).write_bytes(data)
        return {"status": "plain", "ok_pages": 0, "failed_pages": 0}

    salt = data[:16]
    mac_key = derive_mac_key(enc_key, salt)

    total_pages = len(data) // PAGE_SIZE
    if total_pages <= 0:
        raise MacDecryptError("文件长度不足一页")

    # 页1 HMAC 先行校验, 决定密钥是否匹配该库
    if check_hmac:
        iv0 = data[PAGE_SIZE - RESERVE: PAGE_SIZE - RESERVE + IV_SIZE]
        stored0 = data[PAGE_SIZE - RESERVE + IV_SIZE:]
        expect0 = _page_hmac(mac_key, data[16: PAGE_SIZE - RESERVE + IV_SIZE], 1)
        if not hmac_mod.compare_digest(expect0, stored0):
            raise MacDecryptError(
                "密钥不匹配: 第 1 页 HMAC 校验失败(请确认该 enc_key 确为此库的密钥, "
                "且库来自当前登录的微信 4.x)")

    out = bytearray(SQLITE_MAGIC)  # 重建明文头
    ok_pages = 0
    failed = 0
    for n in range(total_pages):
        page = data[n * PAGE_SIZE:(n + 1) * PAGE_SIZE]
        offset = 16 if n == 0 else 0
        iv = page[PAGE_SIZE - RESERVE: PAGE_SIZE - RESERVE + IV_SIZE]
        stored_hmac = page[PAGE_SIZE - RESERVE + IV_SIZE:]
        enc_payload = page[offset: PAGE_SIZE - RESERVE]
        good = True
        if check_hmac:
            expect = _page_hmac(mac_key, page[offset: PAGE_SIZE - RESERVE + IV_SIZE], n + 1)
            good = hmac_mod.compare_digest(expect, stored_hmac)
        try:
            dec = _decrypt_block(enc_key, iv, enc_payload)
        except Exception as e:  # pragma: no cover
            raise MacDecryptError(f"解密出错: {e}")
        # 保留重建后的整页(保留区原样放回), 维持 4096 页对齐
        rebuilt = dec + page[PAGE_SIZE - RESERVE:]
        out += rebuilt
        if good:
            ok_pages += 1
        else:
            failed += 1
            if failed >= 8:
                raise MacDecryptError("密钥不匹配或文件损坏(HMAC 连续失败)")
        if progress and (n % 200 == 0 or n == total_pages - 1):
            progress(n + 1, total_pages)

    Path(dst).write_bytes(bytes(out))
    return {"status": "ok", "ok_pages": ok_pages, "failed_pages": failed}


def sanity_open(db_path: str) -> Tuple[bool, str]:
    """只读打开明文库并返回基础健康信息。"""
    try:
        con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            tables = con.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            con.execute("PRAGMA integrity_check").fetchone()
        finally:
            con.close()
        return True, f"tables={tables}"
    except Exception as e:
        return False, str(e)
