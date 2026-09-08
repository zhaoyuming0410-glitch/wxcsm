"""wxcsm 安装向导（自包含，无需外部安装器如 Inno/NSIS）。

本文件被 PyInstaller 打成「安装程序」exe，运行时从自身尾部读取内嵌的 payload
（zip：启动工具.exe + 卸载脚本），让用户选择安装目录、是否创建桌面快捷方式，
然后解压并建快捷方式。

payload 以固定标记 `WXCSM_PAYLOAD_V2\n` 开头紧跟 zip，拼接在 exe 之后，仅含程序本体。
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import ctypes
import tkinter as tk
import tkinter.ttk as ttk
import tkinter.messagebox as mb
import tkinter.filedialog as fd
import zipfile

PAYLOAD_MARKER = b"WXCSM_PAYLOAD_V2\n"
PAYLOAD_XOR_KEY = 0xAA
APP_EXE = "启动工具.exe"
APP_NAME = "微信客户沟通总结工具"
DEFAULT_DIR = r"C:\Program Files\微信客户沟通总结工具"


def _read_payload_zip() -> "zipfile.ZipFile":
    with open(sys.executable, "rb") as f:
        data = f.read()
    i = data.rfind(PAYLOAD_MARKER)
    if i < 0:
        # 兼容旧版未编码的 V1 payload（仅用于过渡）
        i = data.rfind(b"WXCSM_PAYLOAD_V1\n")
        if i < 0:
            raise RuntimeError("未找到内嵌安装数据（这不是通过 make_installer 生成的安装包）。")
        raw = data[i + len(b"WXCSM_PAYLOAD_V1\n"):]
    else:
        raw = bytes(b ^ PAYLOAD_XOR_KEY for b in data[i + len(PAYLOAD_MARKER):])
    return zipfile.ZipFile(io.BytesIO(raw))


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} GB"


class Installer(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} 安装向导")
        self.geometry("520x320")
        self.minsize(480, 300)
        self.resizable(False, False)

        try:
            self._zf = _read_payload_zip()
            self._total = sum(info.file_size for info in self._zf.infolist())
        except Exception as e:
            mb.showerror("安装包损坏", str(e))
            self.destroy()
            return

        self.var_dir = tk.StringVar(value=DEFAULT_DIR)
        self.var_desktop = tk.BooleanVar(value=True)
        self.var_menu = tk.BooleanVar(value=True)
        self._installed = False

        self._build_ui()

    def _build_ui(self) -> None:
        pad = dict(padx=16, pady=6)
        ttk.Label(self, text=f"欢迎安装 {APP_NAME}", font=("微软雅黑", 14, "bold")
                  ).pack(anchor="w", **pad)
        ttk.Label(self, text="本工具用于客户微信沟通记录总结，生成 Excel 三列标准输出。\n"
                              "点击「安装」即把程序解压到下方目录。",
                  foreground="#6B7280", font=("微软雅黑", 9)
                  ).pack(anchor="w", **pad)

        # 安装目录
        fdir = ttk.Frame(self)
        fdir.pack(fill="x", **pad)
        ttk.Label(fdir, text="安装位置：").pack(side="left")
        ttk.Entry(fdir, textvariable=self.var_dir).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(fdir, text="浏览…", width=8, command=self._browse).pack(side="left")

        # 选项
        fopt = ttk.Frame(self)
        fopt.pack(fill="x", **pad)
        ttk.Checkbutton(fopt, text="创建桌面快捷方式", variable=self.var_desktop).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(fopt, text="创建开始菜单快捷方式", variable=self.var_menu).pack(side="left")

        ttk.Label(self, text=f"所需空间约 {human(self._total)}。",
                  foreground="#6B7280", font=("微软雅黑", 9)
                  ).pack(anchor="w", **pad)

        # 进度
        self.pb = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.pb.pack(fill="x", **pad)
        self.var_msg = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.var_msg, foreground="#6B7280", font=("微软雅黑", 9)
                  ).pack(anchor="w", **pad)

        # 按钮
        fbtn = ttk.Frame(self)
        fbtn.pack(fill="x", side="bottom", **pad)
        self.btn_install = ttk.Button(fbtn, text="安装", command=self._install)
        self.btn_install.pack(side="right", padx=6)
        ttk.Button(fbtn, text="退出", command=self.destroy).pack(side="right")

    def _browse(self) -> None:
        d = fd.askdirectory(initialdir=self.var_dir.get() or DEFAULT_DIR,
                             title="选择安装目录")
        if d:
            self.var_dir.set(d)

    def _install(self) -> None:
        dest = self.var_dir.get().strip()
        if not dest:
            mb.showerror("请选择安装目录", "安装位置不能为空。")
            return
        self.btn_install.configure(state="disabled")
        try:
            self._extract(dest)
            if self.var_desktop.get():
                self._make_shortcut(dest, "Desktop")
            if self.var_menu.get():
                self._make_shortcut(dest, "StartMenu")
            self._installed = True
            mb.showinfo("安装完成",
                        f"{APP_NAME} 已安装到：\n{dest}\n\n"
                        f"可双击「{APP_EXE}」或通过快捷方式启动。\n"
                        f"需要卸载时运行目录内的「卸载.bat」即可。")
            self.var_msg.set("安装完成。")
        except Exception as e:
            mb.showerror("安装失败", f"{e}\n\n请尝试换个有写入权限的目录（如 D 盘）。")
            self.var_msg.set("安装失败。")
        finally:
            self.btn_install.configure(state="normal")

    def _extract(self, dest: str) -> None:
        os.makedirs(dest, exist_ok=True)
        names = self._zf.namelist()
        done = 0
        for name in names:
            target = os.path.join(dest, name)
            if name.endswith("/"):
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with self._zf.open(name) as src, open(target, "wb") as out:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
            done += self._zf.getinfo(name).file_size
            pct = int(done / self._total * 100) if self._total else 100
            self.pb.configure(value=pct)
            self.var_msg.set(f"正在解压：{name}  ({pct}%)")
            self.update()
        self.pb.configure(value=100)

    # ---------------- 快捷方式 ----------------
    def _make_shortcut(self, dest: str, where: str) -> None:
        """where: 'Desktop' | 'StartMenu'。用真实(可能被 OneDrive 重定向的)路径。"""
        target = os.path.join(dest, APP_EXE)
        if where == "Desktop":
            folder = _shell_folder(0x0010)   # CSIDL_DESKTOPDIRECTORY(真实桌面,含重定向)
        else:
            folder = _shell_folder(0x0002)   # CSIDL_PROGRAMS(开始菜单程序)
        if not folder:
            # 兜底 USERPROFILE
            folder = os.path.join(os.environ.get("USERPROFILE", "C:\\"), "Desktop") \
                if where == "Desktop" else os.path.join(
                    os.environ.get("USERPROFILE", "C:\\"), "AppData", "Roaming",
                    "Microsoft", "Windows", "Start Menu", "Programs")
        if not os.path.isdir(folder):
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError:
                return
        link = os.path.join(folder, f"{APP_NAME}.lnk")
        self._create_lnk(link, target, dest)

    def _create_lnk(self, link: str, target: str, workdir: str) -> None:
        """创建 .lnk。用临时 .ps1 脚本避免内联命令的引号转义问题(之前 os.system 里
        双层双引号会让 PowerShell 解析失败,快捷方式被静默丢弃)。路径用单引号包裹。"""
        ps_body = (
            "$ws = New-Object -ComObject WScript.Shell\r\n"
            "$s = $ws.CreateShortcut('{0}')\r\n"
            "$s.TargetPath = '{1}'\r\n"
            "$s.WorkingDirectory = '{2}'\r\n"
            "$s.Description = '{3}'\r\n"
            "$s.Save()\r\n"
        ).format(link, target, workdir, APP_NAME)
        ps_file = os.path.join(tempfile.gettempdir(), "wxcsm_mklnk.ps1")
        # 必须用 utf-8-sig(带 BOM):ps1 含中文路径,PowerShell 把无 BOM 的 UTF-8
        # 当 ANSI(GBK) 解码会乱码,导致 CreateShortcut 静默失败、快捷方式不生成。
        with open(ps_file, "w", encoding="utf-8-sig") as f:
            f.write(ps_body)
        try:
            _ = os.system(
                'powershell -NoProfile -ExecutionPolicy Bypass -File "{0}"'
                .format(ps_file))
        finally:
            try:
                os.remove(ps_file)
            except OSError:
                pass


def _shell_folder(csidl: int) -> str:
    """取真实 Shell 文件夹路径(支持 OneDrive 桌面重定向)。纯 ctypes,不依赖 pywin32。
    csidl: CSIDL_DESKTOPDIRECTORY=0x10 | CSIDL_PROGRAMS=0x02 | 等。失败返回 ''。"""
    try:
        from ctypes import wintypes as _wt
        shl = ctypes.windll.shell32
        buf = ctypes.create_unicode_buffer(260)
        # SHGetFolderPathW(hwnd, csidl, hToken, dwFlags, pszPath)
        shl.SHGetFolderPathW.argtypes = [_wt.HWND, ctypes.c_int, _wt.HANDLE,
                                         _wt.DWORD, _wt.LPWSTR]
        shl.SHGetFolderPathW.restype = ctypes.c_long
        if shl.SHGetFolderPathW(None, csidl, None, 0, buf) == 0 and buf.value:
            return buf.value
    except Exception:
        pass
    return ""


if __name__ == "__main__":
    Installer().mainloop()
