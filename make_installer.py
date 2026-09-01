"""生成可分发安装包：微信客户沟通总结工具_Setup.exe。

流程：
  1) 用 PyInstaller 把 installer_main.py 打成单文件窗口 exe（请求管理员权限，便于写 Program Files）。
  2) 把 启动工具.exe + tools/wechatdataanalysis/* (WDA 安装包) + 卸载.bat 打成 payload.zip（仅存储，不二次压缩）。
  3) 拼接：installer.exe + 标记 + payload.zip  →  微信客户沟通总结工具_Setup.exe。

产物位于 dist_installer/，可直接发给他人在其他电脑双击安装。
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PY = "C:/Users/Administrator/.workbuddy/binaries/python/envs/wxcsm/Scripts/python.exe"
PYINSTALLER = "C:/Users/Administrator/.workbuddy/binaries/python/envs/wxcsm/Scripts/pyinstaller.exe"
APP_EXE = os.path.join(HERE, "dist", "启动工具.exe")
WDA_DIR = os.path.join(HERE, "tools", "wechatdataanalysis")
OUT_DIR = os.path.join(HERE, "dist_installer")
MARKER = b"WXCSM_PAYLOAD_V2\n"
PAYLOAD_XOR_KEY = 0xAA
UNINSTALL_BAT = (
    "@echo off\r\n"
    "echo 正在卸载微信客户沟通总结工具…\r\n"
    "echo 请先关闭正在运行的启动工具.exe\r\n"
    "pause\r\n"
    'rmdir /s /q "%~dp0"\r\n'
    "echo 已卸载。桌面/开始菜单快捷方式请手动删除。\r\n"
    "pause\r\n"
)


def _verify_entry_script(exe_path: str, script_name: str) -> bool:
    """校验 PyInstaller onefile 的入口脚本名称，防止缓存混淆导致打错包。"""
    try:
        from PyInstaller.archive.readers import CArchiveReader
        arch = CArchiveReader(exe_path)
        return script_name in arch.toc
    except Exception:
        return False


def main() -> int:
    include_wda = "--no-wda" not in sys.argv
    if not os.path.isfile(APP_EXE):
        print(f"[错误] 找不到 {APP_EXE}，请先运行 PyInstaller 构建启动工具.exe", file=sys.stderr)
        return 1
    os.makedirs(OUT_DIR, exist_ok=True)

    # 1) 构建安装向导 exe（仅构建一次，两种版本共用同一向导）
    # 先彻底清理旧的 _installer_tmp 构建产物，防止 PyInstaller 复用缓存导致入口脚本被混淆为 app.py
    print("[1/3] PyInstaller 构建安装向导…")
    for stale in (
        os.path.join(OUT_DIR, "_installer_tmp.exe"),
        os.path.join(OUT_DIR, "_installer_tmp.spec"),
        os.path.join(OUT_DIR, "build", "_installer_tmp"),
    ):
        if os.path.isfile(stale):
            os.remove(stale)
        elif os.path.isdir(stale):
            shutil.rmtree(stale)
    subprocess.run(
        [PYINSTALLER, "installer_main.py", "--onefile", "--windowed",
         "--uac-admin", "--name", "_installer_tmp", "--distpath", OUT_DIR,
         "--workpath", os.path.join(OUT_DIR, "build"), "--specpath", OUT_DIR,
         "--noconfirm", "--clean"],
        cwd=HERE, check=True,
    )
    installer_exe = os.path.join(OUT_DIR, "_installer_tmp.exe")
    if not os.path.isfile(installer_exe):
        print("[错误] 安装向导 exe 未生成", file=sys.stderr)
        return 1
    if not _verify_entry_script(installer_exe, "installer_main"):
        print("[错误] 安装向导 exe 入口脚本校验失败（可能被混淆为 app.py），请清理 build 缓存后重试。",
              file=sys.stderr)
        return 1

    # 2) 打包 payload.zip
    wda_files = []
    if include_wda:
        for fn in sorted(os.listdir(WDA_DIR)):
            p = os.path.join(WDA_DIR, fn)
            if os.path.isfile(p) and fn.lower().endswith(".exe"):
                wda_files.append((p, os.path.join("tools", "wechatdataanalysis", fn)))
    print(f"[2/3] 打包内嵌数据（启动工具" + (" + WeChatDataAnalysis 安装包" if include_wda else "，不含 WDA 安装包") + "）…")
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_STORED) as z:
        z.write(APP_EXE, APP_EXE.split(os.sep)[-1])
        for p, arc in wda_files:
            z.write(p, arc)
        z.writestr("卸载.bat", UNINSTALL_BAT)
    payload_bytes = payload.getvalue()
    print(f"      payload 大小：{len(payload_bytes)/1024/1024:.1f} MB")

    # 3) 拼接
    print("[3/3] 拼接为安装包…")
    final = os.path.join(OUT_DIR, "微信客户沟通总结工具_Setup.exe" if include_wda
                          else "微信客户沟通总结工具_精简版_Setup.exe")
    with open(installer_exe, "rb") as f:
        head = f.read()
    # 对 payload 做 XOR 混淆：防止内嵌的 启动工具.exe 尾部的 PyInstaller CArchive cookie
    # 被外层 Setup.exe 的 bootloader 误识别为自身归档，导致双击直接启动 GUI。
    encoded_payload = bytes(b ^ PAYLOAD_XOR_KEY for b in payload_bytes)
    with open(final, "wb") as f:
        f.write(head)
        f.write(MARKER)
        f.write(encoded_payload)

    # 校验拼接后的 Setup.exe 头部确实等于安装向导，防止头部被其他 exe 污染
    with open(final, "rb") as f:
        head_actual = f.read(len(head))
    if head_actual != head:
        print(f"[错误] {os.path.basename(final)} 头部与安装向导不一致，拼接失败。",
              file=sys.stderr)
        os.remove(final)
        return 1

    size = os.path.getsize(final)
    print(f"✅ 完成：{final}  ({size/1024/1024:.1f} MB)")
    print("   发给他人在其他电脑双击即可安装（可选安装路径 + 桌面/开始菜单快捷方式）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
