"""WeChatDataAnalysis 启动器（wxcsm 内置的辅助工具入口，跨平台：Windows + macOS）。

合规边界：wxcsm 不内置 / 不分发解密二进制。这里只做两件事，不下载、不解密：
  1) 发现本机已安装的 WeChatDataAnalysis 并后台拉起；
  2) 若未安装，打开用户自备在 tools/wechatdataanalysis/ 下的安装包（Windows: Setup.exe；
     macOS: .dmg / .pkg / .zip）。

平台差异通过 sys.platform 分派：
  - Windows：注册表 + 全盘 Program Files 探测 .exe；安装包为 WeChatDataAnalysis-*-Setup.exe。
  - macOS：   /Applications、~/Applications 探测 .app；安装包为 WeChatDataAnalysis*.{dmg,pkg,zip}，
             启动与打开安装包统一用 `open` 命令。
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys
from typing import List, Optional, Tuple

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

WDA_APP_NAME = "WeChatDataAnalysis"

# 安装包候选文件名（tools/wechatdataanalysis/ 下）
_WIN_INSTALLER_GLOB = "WeChatDataAnalysis-*-Setup.exe"
_MAC_INSTALLER_GLOBS = ("WeChatDataAnalysis*.dmg", "WeChatDataAnalysis*.pkg", "WeChatDataAnalysis*.zip")

# tools/ 安装包的候选项目根。frozen 单文件 exe / .app 时 sys.executable 可能是副本，
# 故除 exe 同目录外，再回退若干已知项目根（命中 tools/wechatdataanalysis 即用）。
_WIN_ROOTS = [
    os.path.dirname(sys.executable),          # 主：exe 所在目录（wxcsm/ 或 Desktop/ 副本）
    os.getcwd(),
    "D:/WorkBuddy/CSM/wxcsm",
    "C:/WorkBuddy/CSM/wxcsm",
    os.path.expanduser("~/Documents/WorkBuddy/CSM/wxcsm"),
]
_MAC_ROOTS = [
    os.path.dirname(sys.executable),          # .app 内：Contents/MacOS
    os.path.expanduser("~/Applications"),      # 用户级 Applications
    "/Applications",
    os.getcwd(),
    os.path.expanduser("~/WorkBuddy/CSM/wxcsm"),
    os.path.expanduser("~/Documents/WorkBuddy/CSM/wxcsm"),
]


def tools_root() -> str:
    """返回 wxcsm/tools 目录（优先 exe 同级并确认含 wechatdataanalysis，否则回退已知项目根）。"""
    # 1) frozen 场景：.app 内 Contents/Resources 或 PyInstaller 的 _MEIPASS
    res = _frozen_resources_dir()
    if res:
        return res
    # 2) 项目根列表
    for base in (_MAC_ROOTS if IS_MAC else _WIN_ROOTS):
        if not base:
            continue
        d = os.path.join(base, "tools")
        if os.path.isdir(os.path.join(d, "wechatdataanalysis")):
            return d
    # 回退：exe 同级的 tools（即使安装包还没放，也给出确定路径供提示）
    return os.path.join(os.path.dirname(sys.executable), "tools")


def _frozen_resources_dir() -> Optional[str]:
    """frozen 运行时，WeChatDataAnalysis 安装包可能被打进 .app 的 Contents/Resources
    或 PyInstaller 的 _MEIPASS 临时目录。命中即返回该目录。"""
    exe = sys.executable
    # macOS .app: <executable 位于 .../Contents/MacOS/xxx>，Resources 在同级
    if exe and "Contents/MacOS" in exe:
        cand = os.path.normpath(os.path.join(os.path.dirname(exe), "..", "Resources"))
        if os.path.isdir(os.path.join(cand, "wechatdataanalysis")):
            return cand
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass and os.path.isdir(os.path.join(meipass, "wechatdataanalysis")):
        return meipass
    return None


def _wda_pkg_dir() -> str:
    return os.path.join(tools_root(), "wechatdataanalysis")


def find_installer() -> Optional[str]:
    """在 tools/wechatdataanalysis/ 下找安装包，取最新修改的。

    Windows: WeChatDataAnalysis-*-Setup.exe
    macOS:   WeChatDataAnalysis*.dmg / .pkg / .zip
    """
    pkg_dir = _wda_pkg_dir()
    if IS_MAC:
        hits: List[str] = []
        for g in _MAC_INSTALLER_GLOBS:
            hits += glob.glob(os.path.join(pkg_dir, g))
    else:
        hits = glob.glob(os.path.join(pkg_dir, _WIN_INSTALLER_GLOB))
    if not hits:
        return None
    return sorted(hits, key=os.path.getmtime, reverse=True)[0]


# ----------------------------------------------------------------------------
# Windows 专属探测
# ----------------------------------------------------------------------------
def _all_program_dirs() -> List[str]:
    """枚举本机所有固定盘符下的 Program Files / Program Files (x86) 目录。"""
    dirs: List[str] = []
    seen = set()
    # 环境变量指向的（通常是系统盘）
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        v = os.environ.get(env)
        if v and os.path.isdir(v) and v not in seen:
            dirs.append(v)
            seen.add(v)
    # 其余盘符（C:/ D:/ ...）也补上 Program Files 变体
    try:
        import win32api  # type: ignore
        drives = [f"{d}:\\" for d in win32api.GetLogicalDriveStrings().split("\0") if d]
    except Exception:
        drives = [f"{d}:\\" for d in "CDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.isdir(f"{d}:\\")]
    for d in drives:
        for sub in ("Program Files", "Program Files (x86)"):
            p = os.path.join(d, sub)
            if os.path.isdir(p) and p not in seen:
                dirs.append(p)
                seen.add(p)
    return dirs


def _scan_registry() -> List[str]:
    """从 Windows 卸载表找 WeChatDataAnalysis 安装目录（返回候选目录列表）。

    兼容 InstallLocation 为空的情况：DisplayName 命中后，除了读 InstallLocation，
    还会回退到「所有 Program Files 下的同名目录」去定位 exe。
    """
    dirs: List[str] = []
    try:
        import winreg
    except ImportError:
        return dirs
    roots = [
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hkey, sub in roots:
        try:
            key = winreg.OpenKey(hkey, sub)
        except OSError:
            continue
        for i in range(winreg.QueryInfoKey(key)[0]):
            name = winreg.EnumKey(key, i)
            try:
                sk = winreg.OpenKey(key, name)
                disp, _ = winreg.QueryValueEx(sk, "DisplayName")
            except OSError:
                continue
            if "WeChatDataAnalysis" in disp or "微信数据分析" in disp:
                try:
                    loc, _ = winreg.QueryValueEx(sk, "InstallLocation")
                except OSError:
                    loc = ""
                if loc and os.path.isdir(loc):
                    dirs.append(loc)
                # InstallLocation 为空时，回退扫所有 Program Files 下的同名目录
                if not (loc and os.path.isdir(loc)):
                    for pf in _all_program_dirs():
                        cand = os.path.join(pf, "WeChatDataAnalysis")
                        if os.path.isdir(cand):
                            dirs.append(cand)
    return dirs


def _first_exe_in(directory: str) -> Optional[str]:
    """在 directory 内（含一层子目录）返回第一个最像 WeChatDataAnalysis 的可执行文件。"""
    if not directory or not os.path.isdir(directory):
        return None
    exact = glob.glob(os.path.join(directory, "WeChatDataAnalysis*.exe"))
    if exact:
        return sorted(exact, key=os.path.getsize, reverse=True)[0]
    for p in glob.glob(os.path.join(directory, "*.exe")):
        return p
    for p in glob.glob(os.path.join(directory, "*", "*.exe")):
        return p
    return None


# ----------------------------------------------------------------------------
# macOS 专属探测
# ----------------------------------------------------------------------------
def _mac_installed_candidates() -> List[str]:
    """macOS 上已装的 WeChatDataAnalysis.app 候选路径。"""
    bases = ["/Applications", os.path.expanduser("~/Applications")]
    out: List[str] = []
    for b in bases:
        if not b:
            continue
        out.append(os.path.join(b, f"{WDA_APP_NAME}.app"))
    return out


def _is_wda_app(directory: str) -> bool:
    """directory 是否为一个像样的 WeChatDataAnalysis.app（含 Contents/MacOS）。"""
    if not directory.endswith(".app"):
        return False
    if not os.path.isdir(os.path.join(directory, "Contents", "MacOS")):
        return False
    return WDA_APP_NAME.split(".")[0].lower() in os.path.basename(directory).lower()


# ----------------------------------------------------------------------------
# 统一入口
# ----------------------------------------------------------------------------
def find_wda_exe() -> Optional[str]:
    """探测本机已安装的 WeChatDataAnalysis 可执行/可启动项，找不到返回 None。

    Windows 返回 exe 路径；macOS 返回 .app 路径（用 `open` 启动）。
    """
    if IS_WIN:
        candidates: List[str] = list(_scan_registry())
        appdata = os.environ.get("LOCALAPPDATA", os.path.expanduser("~/AppData/Local"))
        for pf in _all_program_dirs():
            candidates.append(os.path.join(pf, "WeChatDataAnalysis"))
        candidates += [
            os.path.join(appdata, "Programs", "WeChatDataAnalysis"),   # Electron 默认
            os.path.join(appdata, "WeChatDataAnalysis"),
            os.path.expandvars(r"%ProgramFiles%\WeChatDataAnalysis"),
            os.path.expandvars(r"%ProgramFiles(x86)%\WeChatDataAnalysis"),
            os.path.join(_wda_pkg_dir(), "app"),                       # 若用户装到 tools 下
        ]
        seen = set()
        uniq = []
        for d in candidates:
            if d and d not in seen:
                seen.add(d)
                uniq.append(d)
        for d in uniq:
            e = _first_exe_in(d)
            if e:
                return e
        return None
    else:
        # macOS：直接查 .app 是否存在
        for app in _mac_installed_candidates():
            if os.path.isdir(app) and _is_wda_app(app):
                return app
        return None


def detect_status() -> Tuple[str, Optional[str]]:
    """返回 (状态, 路径)：("installed", exe/app) | ("not_installed", installer) | ("missing", None)。"""
    exe = find_wda_exe()
    if exe:
        return ("installed", exe)
    inst = find_installer()
    if inst:
        return ("not_installed", inst)
    return ("missing", None)


def installer_hint() -> str:
    """给 UI 用的「缺安装包时该放什么文件」提示文案（平台相关）。"""
    if IS_MAC:
        return "WeChatDataAnalysis 的 .dmg / .pkg 安装包"
    return "WeChatDataAnalysis 的 Setup.exe"


def _detach(args: List[str], cwd: Optional[str] = None) -> subprocess.Popen:
    """后台拉起一个不阻塞 GUI 的进程。

    Windows 下归入新进程组，避免信号串扰；macOS 无此 flag。
    """
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if IS_WIN else 0
    return subprocess.Popen(
        args, cwd=cwd, creationflags=flags,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
    )


def launch_wda() -> dict:
    """点击按钮的统一入口，返回动作结果 dict：

      {"action": "launched",   "path": exe/app, "name": 显示名}
      {"action": "installer",  "path": installer}
      {"action": "no_installer"}
    """
    exe = find_wda_exe()
    if exe:
        if IS_MAC:
            _detach(["open", exe])
        else:
            _detach([exe], cwd=os.path.dirname(exe))
        return {"action": "launched", "path": exe, "name": os.path.basename(exe)}
    inst = find_installer()
    if inst:
        if IS_MAC:
            # `open` 对 .dmg 会挂载并显示 Finder 窗口；对 .pkg 会拉起安装向导
            _detach(["open", inst])
        else:
            _detach([inst], cwd=os.path.dirname(inst))
        return {"action": "installer", "path": inst}
    return {"action": "no_installer"}
