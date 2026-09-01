"""wxcsm macOS 安装向导（自包含，无需外部安装器）。

本文件被 PyInstaller 打成「安装向导」.app，运行时从自身可执行文件尾部读取内嵌的 payload
（zip：启动工具.app + tools/wechatdataanalysis/<WDA 安装包>），让用户选择安装目录、
是否创建桌面替身，然后解压 .app 并建替身。

分发形态（macOS 习惯）：
  - 本向导 .app 双击即运行（无需管理员，默认装到 ~/Applications）。
  - 真正业务程序是 payload 里的「启动工具.app」，安装后用户双击它或用桌面替身启动。
  - WeChatDataAnalysis 安装包一并内嵌，由「启动工具.app」里的按钮负责拉起/打开安装。

payload 以固定标记 `WXCSM_PAYLOAD_V2\n` 开头紧跟 XOR(0xAA) 编码的 zip，拼接在 Mach-O 可执行尾部。
（XOR 是为了防止内嵌 启动工具.app 尾部的 PyInstaller CArchive cookie 被本向导 bootloader 误识别。）
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tkinter as tk
import tkinter.ttk as ttk
import tkinter.messagebox as mb
import tkinter.filedialog as fd
import zipfile

PAYLOAD_MARKER = b"WXCSM_PAYLOAD_V2\n"
PAYLOAD_XOR_KEY = 0xAA
APP_BUNDLE = "启动工具.app"
APP_NAME = "微信客户沟通总结工具"
# 默认装到用户级 Applications（无需管理员密码）；用户也可浏览到 /Applications
DEFAULT_DIR = os.path.expanduser("~/Applications")


def _read_payload_zip() -> "zipfile.ZipFile":
    with open(sys.executable, "rb") as f:
        data = f.read()
    i = data.rfind(PAYLOAD_MARKER)
    if i < 0:
        i = data.rfind(b"WXCSM_PAYLOAD_V1\n")
        if i < 0:
            raise RuntimeError("未找到内嵌安装数据（这不是通过 make_macos 生成的安装包）。")
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
        self.geometry("520x340")
        self.minsize(480, 320)
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
        self._installed = False

        self._build_ui()

    def _build_ui(self) -> None:
        pad = dict(padx=16, pady=6)
        ttk.Label(self, text=f"欢迎安装 {APP_NAME}", font=("PingFang SC", 14, "bold")
                  ).pack(anchor="w", **pad)
        ttk.Label(self, text="本工具用于客户微信沟通记录总结，生成 Excel 三列标准输出。\n"
                              "点击「安装」即把程序复制到下方目录（默认 ~/Applications）。",
                  foreground="#6B7280", font=("PingFang SC", 9)
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
        ttk.Checkbutton(fopt, text="创建桌面替身", variable=self.var_desktop).pack(side="left")

        ttk.Label(self, text=f"所需空间约 {human(self._total)}（含 WeChatDataAnalysis 安装包）。",
                  foreground="#6B7280", font=("PingFang SC", 9)
                  ).pack(anchor="w", **pad)

        # 进度
        self.pb = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.pb.pack(fill="x", **pad)
        self.var_msg = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.var_msg, foreground="#6B7280", font=("PingFang SC", 9)
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
        folder = self.var_dir.get().strip()
        if not folder:
            mb.showerror("请选择安装目录", "安装位置不能为空。")
            return
        dest_app = os.path.join(folder, f"{APP_NAME}.app")
        self.btn_install.configure(state="disabled")
        try:
            os.makedirs(folder, exist_ok=True)
            self._extract(dest_app)
            if self.var_desktop.get():
                self._make_alias(dest_app)
            self._installed = True
            mb.showinfo("安装完成",
                        f"{APP_NAME} 已安装到：\n{dest_app}\n\n"
                        f"• 双击「{APP_NAME}.app」即可启动；若创建了桌面替身，也可双击桌面上的替身。\n"
                        f"• 需要卸载时，运行 {APP_NAME}.app 内的「卸载.command」即可。\n"
                        f"• WeChatDataAnalysis 取数工具未内置：首次使用在点「启动 WeChatDataAnalysis」按钮，"
                        f"会打开内嵌安装包，按提示装到 /Applications 即可。")
            self.var_msg.set("安装完成。")
        except Exception as e:
            mb.showerror("安装失败", f"{e}\n\n若装到 /Applications 提示权限不足，请改用 ~/Applications（默认），"
                                     "或在「浏览…」里选择你有写入权限的目录。")
            self.var_msg.set("安装失败。")
        finally:
            self.btn_install.configure(state="normal")

    def _extract(self, dest_app: str) -> None:
        """把 payload 里的 启动工具.app 解压到 dest_app（覆盖同名）。"""
        if os.path.exists(dest_app):
            import shutil
            shutil.rmtree(dest_app)
        names = self._zf.namelist()
        done = 0
        for name in names:
            # payload 内条目形如 启动工具.app/Contents/...
            target = os.path.join(dest_app, *name.split("/")) if "/" in name else dest_app
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
        # zip 不保留 Unix 权限：恢复 .app 内 Mach-O 可执行位与卸载脚本可执行位
        self._fix_perms(dest_app)

    def _fix_perms(self, dest_app: str) -> None:
        """恢复关键文件的可执行位：Contents/MacOS 下的可执行文件、卸载.command。"""
        macos_dir = os.path.join(dest_app, "Contents", "MacOS")
        if os.path.isdir(macos_dir):
            for fn in os.listdir(macos_dir):
                p = os.path.join(macos_dir, fn)
                if os.path.isfile(p):
                    try:
                        os.chmod(p, 0o755)
                    except OSError:
                        pass
        for root, _dirs, files in os.walk(dest_app):
            for fn in files:
                if fn == "卸载.command":
                    try:
                        os.chmod(os.path.join(root, fn), 0o755)
                    except OSError:
                        pass

    def _make_alias(self, dest_app: str) -> None:
        """在桌面创建指向 .app 的替身（优先 Finder 原生别名，失败回退符号链接）。"""
        desktop = os.path.expanduser("~/Desktop")
        os.makedirs(desktop, exist_ok=True)
        alias_name = f"{APP_NAME}.app"
        target = os.path.join(desktop, alias_name)
        # 先清掉旧的
        if os.path.islink(target) or os.path.exists(target):
            try:
                if os.path.islink(target):
                    os.remove(target)
                else:
                    import shutil
                    shutil.rmtree(target)
            except OSError:
                pass
        # 用 osascript 让 Finder 建真正的替身（双击即启动，且跟随原 app 移动）
        try:
            script = (
                f'tell application "Finder"\n'
                f'  make alias to POSIX file "{dest_app}" at POSIX file "{desktop}"\n'
                f'end tell'
            )
            subprocess.run(["osascript", "-e", script], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # Finder 建的替身默认名为「<app> 的替身」，重命名为统一名
            src = os.path.join(desktop, f"{alias_name} 的替身")
            if os.path.exists(src):
                try:
                    os.rename(src, target)
                except OSError:
                    pass
            return
        except Exception:
            pass
        # 回退：符号链接（Finder 中双击同样能启动 .app）
        try:
            os.symlink(dest_app, target)
        except OSError:
            pass


if __name__ == "__main__":
    Installer().mainloop()
