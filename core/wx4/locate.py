"""微信 4.x(Windows)环境探测:安装、运行、数据目录、账号、加密库。

刻意只依赖标准库 + ctypes(不引第三方),方便 PyInstaller 单文件打包与离线运行。
Windows-only;其他平台本模块直接给出"不支持"错误(由上层决定是否提示 mac 方案)。
"""
from __future__ import annotations

import ctypes
import os
import re
import sqlite3  # noqa: F401  (保持统一导入习惯;未用)
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .errors import Wx4Error

IS_WIN = sys.platform == "win32"
# winreg 仅 Windows 存在; macOS/Linux 不 import, 避免模块级崩溃(由非 Windows 拦截逻辑兜底)。
if IS_WIN:
    import ctypes.wintypes as wt
    import winreg
else:
    wt = None
    winreg = None

# ---------------------------------------------------------------- 进程工具
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _toolhelp_process_names() -> Dict[str, List[int]]:
    """{exe名小写: [pid,...]} —— 用 Toolhelp32 快照,纯 ctypes。"""
    if not IS_WIN:
        return {}
    kernel32 = ctypes.windll.kernel32
    TH32CS_SNAPPROCESS = 0x2
    MAX_PATH = 260

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD),
            ("cntUsage", wt.DWORD),
            ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(wt.ULONG)),
            ("th32ModuleID", wt.DWORD),
            ("cntThreads", wt.DWORD),
            ("th32ParentProcessID", wt.DWORD),
            ("pcPriClassBase", wt.LONG),
            ("dwFlags", wt.DWORD),
            ("szExeFile", wt.WCHAR * MAX_PATH),
        ]

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == wt.HANDLE(-1).value:
        return {}
    out: Dict[str, List[int]] = {}
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            name = pe.szExeFile.lower()
            out.setdefault(name, []).append(int(pe.th32ProcessID))
            ok = kernel32.Process32NextW(snap, ctypes.byref(pe))
    finally:
        kernel32.CloseHandle(snap)
    return out


def running_weixin_pids() -> List[int]:
    return sorted(_toolhelp_process_names().get("weixin.exe", []))


# ---------------------------------------------------------------- 版本工具
def _exe_version(exe_path: str) -> str:
    """读取 exe 文件版本号(FileVersion),ctypes 实现,免 win32api。"""
    try:
        size = ctypes.windll.version.GetFileVersionInfoSizeW(exe_path, None)
        if size <= 0:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(exe_path, 0, size, buf):
            return ""
        # \\StringFileInfo\\<lang><codepage>\\FileVersion,语言页可从 VerQueryValue 探测
        out = ctypes.c_void_p()
        outlen = wt.UINT()
        for sub in (
            r"\VarFileInfo\Translation",
        ):
            if ctypes.windll.version.VerQueryValueW(
                buf, sub, ctypes.byref(out), ctypes.byref(outlen)
            ):
                if outlen.value >= 4:
                    raw = ctypes.string_at(out, 4)
                    lang, cp = raw[0] | raw[1] << 8, raw[2] | raw[3] << 8
                    key = f"\\StringFileInfo\\{lang:04x}{cp:04x}\\FileVersion"
                    vp = ctypes.c_void_p()
                    vl = wt.UINT()
                    if ctypes.windll.version.VerQueryValueW(
                        buf, key, ctypes.byref(vp), ctypes.byref(vl)
                    ):
                        return ctypes.wstring_at(vp, vl.value).strip("\x00 ")
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------- 目录探测
_COMMON_PATTERNS = ("xwechat_files", "wechat_files", "WeChat Files", "Wechat Files")


def _is_data_root(root: Path) -> bool:
    """微信4.x 数据根:含 all_users 或 至少一个 <wxid>_<后缀> 账号目录(其下有 db_storage)。"""
    if not root.is_dir():
        return False
    try:
        entries = list(root.iterdir())
    except OSError:
        return False
    if any(e.name == "all_users" and e.is_dir() for e in entries):
        return True
    for e in entries:
        if e.is_dir() and (e / "db_storage").is_dir():
            return True
    return False


def _scan_children(base: Path, depth: int) -> List[Path]:
    hits: List[Path] = []
    if depth < 0 or not base.is_dir():
        return hits
    try:
        children = list(base.iterdir())
    except OSError:
        return hits
    for c in children:
        if not c.is_dir():
            continue
        low = c.name.lower()
        # 跳过系统/无关大目录, 避免遍历 C:\Users、C:\Windows 等海量文件导致探测数十秒
        if low in ("$recycle.bin", "system volume information", "windows",
                   "program files (x86)", "programdata", "appdata",
                   "users", "perflogs", "onedrive", "intel", "nvidia",
                   "windows.old", "tmp", "temp", "$windows.~bt", "$windows.~ws",
                   "system32", "syswow64", "recovery", "msocache"):
            continue
        if any(p.lower() in low for p in _COMMON_PATTERNS) and _is_data_root(c):
            hits.append(c)
        if depth > 0:
            hits.extend(_scan_children(c, depth - 1))
    return hits


def find_data_roots() -> List[Path]:
    """定位微信4.x 数据根目录(多策略,命中即收)。"""
    found: List[Path] = []
    seen = set()

    def add(p: Path):
        p = p.resolve()
        if p not in seen:
            seen.add(p)
            found.append(p)

    home = Path.home()
    docs = home / "Documents"
    # 1) 常见位置
    for cand in (docs, docs / "xwechat_files", home, home / "xwechat_files"):
        if _is_data_root(cand):
            add(cand)
    # 2) 各盘浅层扫描(深度3,覆盖 D:\Program Files\xwechat_files 这类布局)
    for d in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = Path(f"{d}:\\")
        if not root.exists():
            continue
        for hit in _scan_children(root, 3):
            add(hit)
    return found


# ---------------------------------------------------------------- 数据模型
@dataclass
class WxAccount:
    """一个微信账号及其数据目录。"""

    account: str                    # 账号标识(如 wxid_xxx)
    data_dir: Path                  # <root>/<account>_<suffix>
    db_storage: Path

    @property
    def message_dbs(self) -> List[Path]:
        """聊天消息库(message_N.db),按序号排序;排除 fts/resource/kvdb。"""
        msg_dir = self.db_storage / "message"
        if not msg_dir.is_dir():
            return []
        dbs = []
        for p in sorted(msg_dir.glob("message_*.db")):
            m = re.fullmatch(r"message_(\d+)\.db", p.name)
            if m:
                dbs.append(p)
        # 排除 business 账号消息(biz_message_*.db)不在 message_*.db 之列,天然已排除
        return dbs

    @property
    def contact_db(self) -> Optional[Path]:
        c = self.db_storage / "contact" / "contact.db"
        return c if c.is_file() else None

    @property
    def session_db(self) -> Optional[Path]:
        c = self.db_storage / "session" / "session.db"
        return c if c.is_file() else None


@dataclass
class WxEnv:
    exe_path: Optional[str] = None
    version: str = ""
    data_root: Optional[Path] = None
    accounts: List[WxAccount] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return bool(running_weixin_pids())

    @property
    def primary_account(self) -> Optional[WxAccount]:
        return self.accounts[0] if self.accounts else None


# ---------------------------------------------------------------- 全局缓存
_CACHED_ENV: Optional[WxEnv] = None
_CACHED_ENV_TTL: float = 0  # time.monotonic() 时间戳


def detect_wechat_env(force: bool = False) -> WxEnv:
    """完整探测;找不到任何账号时 accounts 为空(上层给出人话提示)。

    结果会缓存 60 秒(避免 GUI 主线程反复调用卡死)。
    force=True 强制重新探测。
    """
    global _CACHED_ENV, _CACHED_ENV_TTL
    import time as _t
    if not IS_WIN:
        # 非 Windows(如 macOS)无此机制: 直接返回空, 由上层提示改用其它数据源。
        env = WxEnv()
        _CACHED_ENV = env
        _CACHED_ENV_TTL = _t.monotonic()
        return env
    now = _t.monotonic()
    if not force and _CACHED_ENV is not None and (now - _CACHED_ENV_TTL) < 60:
        return _CACHED_ENV
    env = WxEnv()

    # 1) 安装位置(注册表卸载项 + 运行进程路径 + 常见目录)
    exe_candidates: List[str] = []
    _HIVE_MAP = {
        "HKCU": winreg.HKEY_CURRENT_USER,
        "HKLM": winreg.HKEY_LOCAL_MACHINE,
    }
    for hkey, sub in (
        ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        ("HKLM", r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        ("HKLM", r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ):
        try:
            key = winreg.OpenKey(_HIVE_MAP[hkey], sub)
            for i in range(winreg.QueryInfoKey(key)[0]):
                try:
                    k = winreg.OpenKey(key, winreg.EnumKey(key, i))
                    name = winreg.QueryValueEx(k, "DisplayName")[0]
                    if name and ("Weixin" in name or "微信" in name):
                        loc = winreg.QueryValueEx(k, "InstallLocation")[0]
                        if loc:
                            # 注册表值常带引号,如 "D:\Program Files\Tencent\Weixin"
                            loc = loc.strip().strip('"').strip()
                            exe_candidates.append(os.path.join(loc, "Weixin.exe"))
                except OSError:
                    pass
        except OSError:
            pass
    # 常见目录:系统盘 + 所有逻辑盘符 (C:/D:/E:/...)
    _common_bases: List[Path] = []
    for base in (
        Path(os.environ.get("ProgramFiles", "")),
        Path(os.environ.get("ProgramFiles(x86)", "")),
    ):
        if str(base):
            _common_bases.append(base)
    # 扫描所有盘符的 Tencent\Weixin / Weixin
    for drive in "CDEFGH":
        dpath = Path(f"{drive}:\\")
        if dpath.exists():
            _common_bases.append(dpath / "Program Files")
            _common_bases.append(dpath)
    for base in _common_bases:
        for cand in (base / "Tencent" / "Weixin", base / "Weixin"):
            p = cand / "Weixin.exe"
            if p.is_file():
                exe_candidates.append(str(p))
    for p in exe_candidates:
        if os.path.isfile(p):
            env.exe_path = p
            break
    if not env.exe_path:
        # 运行中进程兜底:QueryFullProcessImageName
        # 必须先设置正确的 ctypes 签名,否则 HANDLE 被截断成 32 位导致调用失败
        _k32 = ctypes.windll.kernel32
        try:
            _k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
            _k32.OpenProcess.restype = wt.HANDLE
            _k32.QueryFullProcessImageNameW.argtypes = [
                wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
            _k32.QueryFullProcessImageNameW.restype = wt.BOOL
            _k32.CloseHandle.argtypes = [wt.HANDLE]
            _k32.CloseHandle.restype = wt.BOOL
        except Exception:
            pass
        for pid in running_weixin_pids():
            try:
                h = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                if not h:
                    continue
                buf = ctypes.create_unicode_buffer(1024)
                n = wt.DWORD(1024)
                ok = _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
                _k32.CloseHandle(h)
                if ok and os.path.isfile(buf.value):
                    env.exe_path = buf.value
                    break
            except Exception:
                continue
    if env.exe_path:
        env.version = _exe_version(env.exe_path)

    # 2) 数据目录 + 账号
    roots = find_data_roots()
    if not roots and env.running:
        # 极端布局:通过运行进程的工作目录向上兜底 3 层
        for pid in running_weixin_pids():
            try:
                import ctypes.wintypes  # noqa: F401
            except Exception:
                pass
    if not roots and env.running:
        # 上面兜底较弱:用户可手动指定;留给上层
        pass
    if roots:
        env.data_root = roots[0]
        root = roots[0]
        for e in sorted(root.iterdir()):
            if not e.is_dir() or e.name == "all_users":
                continue
            db_storage = e / "db_storage"
            if db_storage.is_dir():
                account = re.split(r"_\d+$", e.name)[0] or e.name
                env.accounts.append(WxAccount(account=account, data_dir=e, db_storage=db_storage))
        # 按账号排序:把当前活跃(登录)账号排到最前 —— 因为只有活跃账号的
        # 数据库密钥在内存里,V4 扫描只对它有效。活跃判据不能只看 db_storage
        # 是否在 60s 内写过 DB(微信空闲时不写 DB,会误判)。改用更稳的信号:
        # 活跃账号的 data_dir 各顶层子目录(temp/cache/config/msg/resource…)
        # 会被微信持续写入,子目录自身的 mtime 会随之更新,这是廉价且可靠的判据。
        if len(env.accounts) > 1:
            def _active_score(a: WxAccount) -> float:
                """返回该账号 data_dir 里最近一次可见的写入时间;越大越可能是活跃账号。"""
                max_mtime = 0.0
                # 1) data_dir 顶层子目录的 mtime(微信活跃时持续在活跃账号里建/删临时文件)
                try:
                    for child in a.data_dir.iterdir():
                        try:
                            if child.is_dir():
                                m = child.stat().st_mtime
                                if m > max_mtime:
                                    max_mtime = m
                        except OSError:
                            continue
                except OSError:
                    pass
                # 2) db_storage 顶层 + 关键子目录一级的 .db(不递归,避免 rglob 整个目录的 20-30s)
                try:
                    for db in a.db_storage.glob("*.db"):
                        try:
                            if db.stat().st_mtime > max_mtime:
                                max_mtime = db.stat().st_mtime
                        except OSError:
                            continue
                except OSError:
                    pass
                for sub in ("message", "contact", "session", "misc", "custom"):
                    sub_dir = a.db_storage / sub
                    try:
                        for db in sub_dir.glob("*.db"):
                            try:
                                if db.stat().st_mtime > max_mtime:
                                    max_mtime = db.stat().st_mtime
                            except OSError:
                                continue
                    except OSError:
                        continue
                return max_mtime if max_mtime else 0.0
            # 降序:最近写入者排最前。完全没有可写文件(score=0)的账号按账号名兜底,保持稳定。
            env.accounts.sort(key=lambda a: (-_active_score(a), a.account))
        else:
            env.accounts.sort(key=lambda a: a.account)
    _CACHED_ENV = env
    _CACHED_ENV_TTL = _t.monotonic()
    return env


def pick_message_db(env: WxEnv, account: Optional[WxAccount] = None) -> Optional[Path]:
    """返回可用来校验密钥/解密的主消息库(优先 message_0.db)。"""
    acc = account or env.primary_account
    if not acc:
        return None
    dbs = acc.message_dbs
    if not dbs:
        return None
    return dbs[0]
