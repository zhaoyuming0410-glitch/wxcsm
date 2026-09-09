"""抓到的数据库密钥本机缓存(仅 Windows,DPAPI 加密,当前用户可解)。

存于 %USERPROFILE%\\.wxcsm\\wx4_keys.bin:
  CryptProtectData( json.dumps({account: meta}) )   —— 只有本机当前 Windows 用户能解开。

这样用户只需在微信登录态下抓一次密钥;之后(即使微信已退出)也能离线解密。
若密钥失效(如账号退出重登换钥),数据源层会检测到并提示重新抓取。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import datetime
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from ..config import CONFIG_DIR

_ENTROPY = b"wxcsm-wx4-keyring-v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_KEY_FILE = CONFIG_DIR / "wx4_keys.bin"


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _protect(data: bytes) -> bytes:
    if sys.platform != "win32":
        raise RuntimeError("DPAPI 加密仅支持 Windows")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in = DATA_BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_char)))
    ent = DATA_BLOB(len(_ENTROPY), ctypes.cast(ctypes.create_string_buffer(_ENTROPY), ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    ok = crypt32.CryptProtectData(ctypes.byref(blob_in), None, ctypes.byref(ent),
                                  None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise RuntimeError(f"CryptProtectData 失败: {ctypes.get_last_error()}")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _unprotect(blob: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in = DATA_BLOB(len(blob), ctypes.cast(ctypes.create_string_buffer(blob), ctypes.POINTER(ctypes.c_char)))
    ent = DATA_BLOB(len(_ENTROPY), ctypes.cast(ctypes.create_string_buffer(_ENTROPY), ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, ctypes.byref(ent),
                                    None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise RuntimeError(f"CryptUnprotectData 失败: {ctypes.get_last_error()}")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _load_all() -> Dict[str, Any]:
    if not _KEY_FILE.exists():
        return {}
    try:
        return json.loads(_unprotect(_KEY_FILE.read_bytes()).decode("utf-8"))
    except Exception:
        return {}


def _save_all(data: Dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    _KEY_FILE.write_bytes(_protect(raw))


def save_key(account: str, key_hex: str, *, wechat_version: str = "",
             wechat_exe: str = "", db_salt_hint: str = "") -> None:
    allk = _load_all()
    allk[account] = {
        "db_key": key_hex.strip().lower(),
        "wechat_version": wechat_version,
        "wechat_exe": wechat_exe,
        "db_salt_hint": db_salt_hint,
        "captured_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    _save_all(allk)


def load_key(account: str) -> Optional[Dict[str, Any]]:
    return _load_all().get(account)


def remove_key(account: str) -> None:
    allk = _load_all()
    if account in allk:
        del allk[account]
        _save_all(allk)


def has_key(account: str) -> bool:
    return bool(_load_all().get(account, {}).get("db_key"))
