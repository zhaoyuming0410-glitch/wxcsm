# -*- coding: utf-8 -*-
"""cipher 模块自洽性测试:加密→解密还原,密钥错误必须被识别。"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import pytest
except ImportError:  # 允许无 pytest 环境直接 python 运行
    import contextlib

    class _Raises:
        def __init__(self, exc): self.exc = exc
        def __enter__(self): return self
        def __exit__(self, et, ev, tb):
            if et is None:
                raise AssertionError(f"should raise {self.exc}")
            if not issubclass(et, self.exc):
                raise AssertionError(f"expected {self.exc}, got {et}: {ev}")
            return True

    class _Module:
        def raises(self, exc): return _Raises(exc)
    pytest = _Module()

from core.wx4 import cipher
from core.wx4.errors import NotDecryptableError


def _encrypt_fixture(plain: bytes, raw_key: bytes, salt: bytes):
    """把 plain(整页字节,页1含 16B 头的布局)按 cipher 的映射加密。

    mapping(与 cipher.decrypt 互逆):
      page1  密文区 = plain[16:4016](4000B)  -> file[16:4016];file[0:16]=salt
      page>1 密文区 = plain[0:4016](4016B)   -> file[0:4016]
      保留区 80B = iv + hmac,hmac 覆盖密文区+iv+页码(LE4)
    """
    page_key = cipher.derive_page_key(raw_key, salt)
    mac_key = cipher.derive_mac_key(page_key, salt)
    import hmac as hmac_mod
    import hashlib
    from Crypto.Cipher import AES

    n_pages = len(plain) // cipher.PAGE_SIZE
    out = bytearray()
    for n in range(n_pages):
        page = plain[n * cipher.PAGE_SIZE:(n + 1) * cipher.PAGE_SIZE]
        iv = os.urandom(16)
        if n == 0:
            payload = page[16:cipher.PAGE_SIZE - cipher.RESERVE]  # [16:4016]
            enc = AES.new(page_key, AES.MODE_CBC, iv).encrypt(payload)
            chunk = enc  # salt is NOT part of ciphertext for HMAC
        else:
            payload = page[0:cipher.PAGE_SIZE - cipher.RESERVE]   # [0:4016]
            enc = AES.new(page_key, AES.MODE_CBC, iv).encrypt(payload)
            chunk = enc
        h = hmac_mod.new(mac_key, digestmod=hashlib.sha512)
        # HMAC: ciphertext + iv + page_number
        h.update(chunk + iv)
        h.update((n + 1).to_bytes(4, "little"))
        if n == 0:
            encrypted_page = salt + chunk
        else:
            encrypted_page = chunk
        encrypted_page += iv + h.digest()
        out += encrypted_page
    return bytes(out)


def _make_plain(n_pages=4):
    pages = bytearray()
    for n in range(n_pages):
        page = bytearray(os.urandom(cipher.PAGE_SIZE))  # 随机内容页
        pages += page
    pages[0:16] = b"SQLite format 3\x00"
    return bytes(pages)


def test_roundtrip():
    key = os.urandom(32)
    salt = os.urandom(16)
    plain = _make_plain(4)
    enc = _encrypt_fixture(plain, key, salt)
    assert enc[:16] == salt
    assert len(enc) == cipher.PAGE_SIZE * 4

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.db"
        dst = Path(td) / "out.db"
        src.write_bytes(enc)
        cipher.decrypt_db_file(str(src), key.hex(), str(dst), check_hmac=True)
        dec = dst.read_bytes()
        # 页1:文件头 + 还原的 plain[16:4016] + 保留区
        assert dec[:16] == b"SQLite format 3\x00"
        assert len(dec) == len(plain)
        # 内容区逐页还原一致(保留区允许不一致——原实现放回密文 iv/hmac)
        for n in range(4):
            base = n * cipher.PAGE_SIZE
            off = 16 if n == 0 else 0
            plen = cipher.PAGE_SIZE - cipher.RESERVE - off
            assert dec[base + (0 if n else 16):base + (0 if n else 16) + plen] == \
                   plain[base + off:base + off + plen], f"page {n} mismatch"


def test_wrong_key_detected():
    key = os.urandom(32)
    wrong = os.urandom(32)
    plain = _make_plain(3)
    enc = _encrypt_fixture(plain, key, os.urandom(16))
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.db"
        dst = Path(td) / "out.db"
        src.write_bytes(enc)
        with pytest.raises(NotDecryptableError):
            cipher.decrypt_db_file(str(src), wrong.hex(), str(dst), check_hmac=True)


def test_plain_passthrough():
    plain = bytearray(_make_plain(2))
    plain[:16] = b"SQLite format 3\x00"
    plain = bytes(plain)
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "p.db"
        dst = Path(td) / "o.db"
        src.write_bytes(plain)
        cipher.decrypt_db_file(str(src), os.urandom(32).hex(), str(dst))
        assert dst.read_bytes() == plain


def test_sanity_open():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "s.db"
        import sqlite3
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE t (a)")
        con.execute("INSERT INTO t VALUES (1)")
        con.commit()
        con.close()
        ok, msg = cipher.sanity_open(str(p))
        assert ok, msg


if __name__ == "__main__":
    for fn in [test_roundtrip, test_wrong_key_detected, test_plain_passthrough, test_sanity_open]:
        name = fn.__name__
        try:
            fn()
            print(f"  PASS  {name}", flush=True)
        except Exception as e:
            print(f"  FAIL  {name}: {e}", flush=True)
