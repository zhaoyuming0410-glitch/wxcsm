"""微信 4.x 加密库解密器(独立实现,不依赖任何第三方取数工具代码)。

加密规范(与公开的 SQLCipher4/微信4.x 社区研究一致,本模块按规范独立实现):
  - 页大小 4096;第一页开头 16 字节为盐(salt),其余页无盐;
  - 每页末尾 80 字节保留区:前 16 字节 IV,后 64 字节 HMAC-SHA512;
  - 页面密钥:PBKDF2-HMAC-SHA512(raw_key32, file_salt, 256000, 32);
  - MAC 密钥:PBKDF2-HMAC-SHA512(page_key, file_salt ^ 0x3a, 2, 32);
  - HMAC 覆盖:密文 + IV + 页码(小端 4 字节),与社区实现一致;
  - AES-256-CBC 解密密文区。

输出为可被 python sqlite3 直读的明文 SQLite 文件(页对齐保持 4096)。
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import sqlite3
from pathlib import Path
from typing import Callable, Optional, Tuple

try:  # 优先用 PyCryptodome;不可用时回退纯 stdlib(hashlib 的 pbkdf2 也有)
    from Crypto.Cipher import AES
    from Crypto.Protocol.KDF import PBKDF2
    from Crypto.Hash import SHA512 as _SHA512
    _HAVE_CRYPTO = True
except Exception:  # pragma: no cover
    _HAVE_CRYPTO = False

from .errors import NotDecryptableError

SQLITE_MAGIC = b"SQLite format 3\x00"
PAGE_SIZE = 4096
RESERVE = 80
IV_SIZE = 16
HMAC_SIZE = 64
KDF_ITER = 256000
MAC_ITER = 2

ProgressFn = Callable[[int, int], None]  # (done_pages, total_pages)


# --------------------------------------------------------------------------- 密钥派生
def derive_page_key(raw_key: bytes, salt: bytes) -> bytes:
    """raw_key: 32B; salt: 文件头 16B。返回 32B 页面 AES 密钥。"""
    if _HAVE_CRYPTO:
        return PBKDF2(raw_key, salt, dkLen=32, count=KDF_ITER, hmac_hash_module=_SHA512)
    return hashlib.pbkdf2_hmac("sha512", raw_key, salt, KDF_ITER, dklen=32)


def derive_mac_key(page_key: bytes, salt: bytes) -> bytes:
    """返回 HMAC-SHA512 用的 32B 密钥。"""
    mac_salt = bytes(b ^ 0x3A for b in salt)
    if _HAVE_CRYPTO:
        return PBKDF2(page_key, mac_salt, dkLen=32, count=MAC_ITER, hmac_hash_module=_SHA512)
    return hashlib.pbkdf2_hmac("sha512", page_key, mac_salt, MAC_ITER, dklen=32)


def parse_key(hex_or_bytes) -> bytes:
    if isinstance(hex_or_bytes, (bytes, bytearray)):
        b = bytes(hex_or_bytes)
        if len(b) == 32:
            return b
        raise ValueError("密钥必须是 32 字节")
    s = (hex_or_bytes or "").strip()
    if len(s) != 64:
        raise ValueError("密钥必须是 64 位十六进制(32 字节)")
    try:
        return bytes.fromhex(s)
    except ValueError:
        raise ValueError("密钥不是合法十六进制")


# --------------------------------------------------------------------------- 探测
def looks_encrypted(data: bytes) -> bool:
    """真加密库: 不是明文 sqlite 头; 长度是页大小的整数倍(基本)。"""
    if data[:16] == SQLITE_MAGIC:
        return False
    return True


def _page_hmac(mac_key: bytes, data_with_iv: bytes, page_no: int) -> bytes:
    m = hmac_mod.new(mac_key, digestmod=hashlib.sha512)
    m.update(data_with_iv)
    m.update(page_no.to_bytes(4, "little"))
    return m.digest()


# --------------------------------------------------------------------------- 解密
def decrypt_page(page: bytes, page_no: int, page_key: bytes, mac_key: bytes,
                 offset: int, check_hmac: bool = True,
                 ) -> Tuple[bytes, bool]:
    """解密单页。offset: 第一页为 16(跳过盐),其余 0。
    返回 (重建后的 4096 字节页, HMAC是否通过)。"""
    iv = page[PAGE_SIZE - RESERVE:PAGE_SIZE - RESERVE + IV_SIZE]
    stored_hmac = page[PAGE_SIZE - RESERVE + IV_SIZE:]
    cipher_end = PAGE_SIZE - RESERVE + IV_SIZE          # 4032(密文+IV 到此为止)
    encrypted_payload = page[offset:PAGE_SIZE - RESERVE]  # 密文区
    ok = True
    if check_hmac:
        expect = _page_hmac(mac_key, page[offset:cipher_end], page_no)
        ok = hmac_mod.compare_digest(expect, stored_hmac)
    if _HAVE_CRYPTO:
        dec = AES.new(page_key, AES.MODE_CBC, iv).decrypt(encrypted_payload)
    else:  # pragma: no cover — 理论回退实现
        from cryptography.hazmat.primitives.ciphers import Cipher as _C, algorithms as _A, modes as _M
        dec = _C(_A.AES(page_key), _M.CBC(iv)).decryptor().update(encrypted_payload)
    rebuilt = dec + page[PAGE_SIZE - RESERVE:]          # 保留区原样放回,维持 4096 页对齐
    return rebuilt, ok


def decrypt_db_file(src: str, key_hex: str, dst: str,
                    check_hmac: bool = True,
                    progress: Optional[ProgressFn] = None,
                    ) -> dict:
    """把加密库 src 解密写为明文库 dst。

    返回 {status, ok_pages, failed_pages, error?};HMAC 失败页超过阈值则抛
    NotDecryptableError(密钥错误 / 文件不是该密钥的库)。
    """
    src_p = Path(src)
    data = src_p.read_bytes()
    if len(data) < PAGE_SIZE:
        raise NotDecryptableError("文件太小,不是有效的微信加密库")
    if data[:16] == SQLITE_MAGIC:
        # 已是明文,原样复制
        Path(dst).write_bytes(data)
        return {"status": "plain", "ok_pages": 0, "failed_pages": 0}

    raw_key = parse_key(key_hex)
    salt = data[:16]
    page_key = derive_page_key(raw_key, salt)
    mac_key = derive_mac_key(page_key, salt)

    total_pages = len(data) // PAGE_SIZE
    if total_pages <= 0:
        raise NotDecryptableError("文件长度不足一页")

    out = bytearray()
    out += SQLITE_MAGIC   # 页1密文不含明文头,明文头在此重建(社区验证格式)
    ok_pages = 0
    failed = 0
    first_bad = None
    for n in range(total_pages):
        page = data[n * PAGE_SIZE:(n + 1) * PAGE_SIZE]
        offset = 16 if n == 0 else 0
        rebuilt, ok = decrypt_page(page, n + 1, page_key, mac_key, offset,
                                   check_hmac=check_hmac)
        if ok:
            ok_pages += 1
        else:
            failed += 1
            if first_bad is None:
                first_bad = n + 1
        if n == 0 and not ok:
            # 首页(唯一承载 SQLite 头信息)HMAC 即败,直接判密钥错,节省时间
            raise NotDecryptableError("密钥不匹配:第一页 HMAC 校验失败")
        if check_hmac and failed >= 8:
            raise NotDecryptableError(f"密钥不匹配或文件损坏:自第 {first_bad} 页起 HMAC 校验失败")
        out += rebuilt
        if progress and (n % 200 == 0 or n == total_pages - 1):
            progress(n + 1, total_pages)
    Path(dst).write_bytes(bytes(out))
    return {"status": "ok", "ok_pages": ok_pages, "failed_pages": failed}


def decrypt_db_file_v2(src, key_hex, dst, check_hmac=True, progress=None) -> dict:
    """v2 重建方式:不保留每页 80B 保留区,输出页为 4016/4000 字节(更接近明文原库)。

    主要供保留区重建(原样放回)在目标库上不可读时选用。"""
    data = Path(src).read_bytes()
    if data[:16] == SQLITE_MAGIC:
        Path(dst).write_bytes(data)
        return {"status": "plain"}
    raw_key = parse_key(key_hex)
    salt = data[:16]
    page_key = derive_page_key(raw_key, salt)
    mac_key = derive_mac_key(page_key, salt)
    total_pages = len(data) // PAGE_SIZE
    out = bytearray(SQLITE_MAGIC)
    ok = fail = 0
    for n in range(total_pages):
        page = data[n * PAGE_SIZE:(n + 1) * PAGE_SIZE]
        offset = 16 if n == 0 else 0
        iv = page[PAGE_SIZE - RESERVE:PAGE_SIZE - RESERVE + IV_SIZE]
        enc = page[offset:PAGE_SIZE - RESERVE]
        stored_hmac = page[PAGE_SIZE - RESERVE + IV_SIZE:]
        good = True
        if check_hmac:
            expect = _page_hmac(mac_key, page[offset:PAGE_SIZE - RESERVE + IV_SIZE], n + 1)
            good = hmac_mod.compare_digest(expect, stored_hmac)
        if _HAVE_CRYPTO:
            dec = AES.new(page_key, AES.MODE_CBC, iv).decrypt(enc)
        else:  # pragma: no cover
            from cryptography.hazmat.primitives.ciphers import Cipher as _C, algorithms as _A, modes as _M
            dec = _C(_A.AES(page_key), _M.CBC(iv)).decryptor().update(enc)
        out += dec if n > 0 else dec[16:]
        if good:
            ok += 1
        else:
            fail += 1
        if fail >= 8:
            raise NotDecryptableError("密钥不匹配或文件损坏(HMAC)")
    Path(dst).write_bytes(bytes(out))
    return {"status": "ok", "ok_pages": ok, "failed_pages": fail}


def sanity_open(db_path: str) -> Tuple[bool, str]:
    """用 sqlite3 尝试打开明文库并做基础校验。"""
    try:
        con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
        try:
            cur = con.execute("PRAGMA integrity_check")
            row = cur.fetchone()
            ok = bool(row) and row[0] == "ok"
            tbl = con.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        finally:
            con.close()
        return ok, f"integrity={row[0] if row else '?'}, tables={tbl}"
    except Exception as e:
        return False, str(e)
