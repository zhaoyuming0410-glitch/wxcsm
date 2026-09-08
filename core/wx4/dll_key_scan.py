"""从 Weixin.dll 中提取 internal_db_key (XOR 密钥)。

快速扫描 .rdata 段中的 32 字节常量候选。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import time
from pathlib import Path
from typing import List, Optional

try:
    import pefile
except ImportError:
    pefile = None


def _get_weixin_dll_path() -> Optional[str]:
    """快速查找 Weixin.dll 路径(从已知位置)。"""
    # 1. 从微信安装目录
    from .locate import detect_wechat_env
    env = detect_wechat_env()
    if env and env.exe_path:
        dll = os.path.join(os.path.dirname(env.exe_path), "Weixin.dll")
        if os.path.isfile(dll):
            return dll
    return None


def extract_xor_keys_from_dll(dll_path: Optional[str] = None) -> List[bytes]:
    """从 Weixin.dll 中提取可能的 XOR key 候选。

    快速扫描 .rdata/.data 段, stride=32, 超时 10 秒。
    """
    if pefile is None:
        return []

    if dll_path is None:
        dll_path = _get_weixin_dll_path()

    if not dll_path or not os.path.isfile(dll_path):
        return []

    try:
        pe = pefile.PE(dll_path)
    except Exception:
        return []

    candidates = []
    t_start = time.time()

    for section in pe.sections:
        name = section.Name.decode("utf-8", errors="ignore").rstrip("\x00")
        if name not in (".rdata", ".data"):
            continue

        data = section.get_data()
        if len(data) < 32:
            continue

        for off in range(0, len(data) - 31, 32):
            if time.time() - t_start > 10:
                break

            chunk = data[off:off + 32]

            if all(b == 0 for b in chunk) or all(b == 0xFF for b in chunk):
                continue

            ascii_printable = sum(1 for b in chunk if 32 <= b <= 126)
            if ascii_printable >= 28:
                continue

            max_run = 1
            run = 1
            for i in range(1, 32):
                if chunk[i] == chunk[i - 1]:
                    run += 1
                    max_run = max(max_run, run)
                else:
                    run = 1
            if max_run >= 8:
                continue

            if sum(1 for b in chunk if b == 0) > 20:
                continue

            # 熵值简化计算
            freq = {}
            for b in chunk:
                freq[b] = freq.get(b, 0) + 1
            entropy = 0
            for c in freq.values():
                p = c / 32
                if p > 0:
                    entropy -= p * __import__("math").log2(p)
            if entropy < 4.0:
                continue

            candidates.append((entropy, off, chunk))

    candidates.sort(key=lambda x: -x[0])
    return [c[2] for c in candidates[:20]]


def xor_raw_key(raw_key: bytes, internal_db_key: bytes) -> bytes:
    """对原始 32 字节候选 key 执行 XOR 变换。"""
    if len(raw_key) != 32:
        raise ValueError(f"raw key length must be 32, got {len(raw_key)}")
    if len(internal_db_key) != 32:
        raise ValueError(f"internal_db_key length must be 32, got {len(internal_db_key)}")
    return bytes(a ^ b for a, b in zip(raw_key, internal_db_key))


def verify_key(candidate: bytes, db_path: str, internal_db_key: Optional[bytes] = None) -> Optional[str]:
    """验证候选密钥是否与数据库匹配。

    尝试两种方式:
      1. AES-CBC 解密检查 SQLite magic
      2. PBKDF2 + HMAC 验证 (SQLCipher4 标准)
    """
    if not os.path.isfile(db_path):
        return None

    try:
        with open(db_path, "rb") as f:
            page1 = f.read(4096)
    except Exception:
        return None

    if len(page1) < 4096:
        return None

    salt = page1[16:32]
    iv = page1[64:80]
    ct = page1[80:96]

    if internal_db_key is not None and len(internal_db_key) == 32:
        key = xor_raw_key(candidate, internal_db_key)
    else:
        key = candidate

    from Crypto.Cipher import AES
    try:
        cipher = AES.new(key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(ct)
        if decrypted[:16] == b"SQLite format 3\x00":
            return key.hex()
    except Exception:
        pass

    try:
        from Crypto.Protocol.KDF import PBKDF2
        from Crypto.Hash import SHA512
        import hmac as hmac_mod

        mac_salt = bytes(b ^ 0x3A for b in salt)
        mac_key = PBKDF2(key, mac_salt, 32, 2,
                         hmac_mod.new(b"", digestmod=SHA512).digestmod)

        stored_hmac = page1[32:64]
        computed_hmac = hmac_mod.new(mac_key, page1[80:], SHA512).digest()
        if computed_hmac == stored_hmac:
            return key.hex()
    except Exception:
        pass

    return None