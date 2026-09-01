"""生成 macOS 可分发安装包（本脚本只能在 macOS 上运行）。

流程：
  1) PyInstaller 把 app.py 打成 启动工具.app（--windowed），并把
     tools/wechatdataanalysis/ 下的 macOS 版 WeChatDataAnalysis 安装包
     （.dmg / .pkg / .zip）通过 --add-data 打进
     启动工具.app/Contents/Resources/wechatdataanalysis/（wda_launcher 会自动找到）。
  2) 注入「卸载.command」到 启动工具.app 顶层（双击即可卸载）。
  3) PyInstaller 把 installer_macos.py 打成安装向导 .app（--windowed）。
  4) 把 启动工具.app 递归打成 payload.zip（ZIP_STORED），XOR(0xAA) 后追加到
     安装向导 .app 的 Mach-O 可执行尾部（标记 WXCSM_PAYLOAD_V2）。
     —— XOR 防止内嵌 启动工具.app 尾部的 PyInstaller CArchive cookie 被向导
        bootloader 误识别（同 Windows 版原理）。
  5) 重命名安装向导 .app 为「微信客户沟通总结工具安装向导.app」。
  6)（可选 --dmg）hdiutil 打成 .dmg 便于分发。

用法（在 macOS 终端，且已激活含 pyinstaller + tkinter 的 venv）：
  python make_macos.py            # 全量：内嵌 WDA 安装包
  python make_macos.py --no-wda  # 精简：不含 WDA 安装包
  python make_macos.py --dmg     # 额外打成 .dmg

注意：Windows 无法生成 macOS 可执行文件，本脚本会直接拒绝运行。
"""
from __future__ import annotations

import io
import os
import shutil
import stat
import subprocess
import sys
import zipfile

if sys.platform != "darwin":
    sys.stderr.write("❌ 本脚本只能在 macOS 上运行（无法在 Windows/Linux 交叉编译 macOS .app）。\n")
    sys.exit(2)

HERE = os.path.dirname(os.path.abspath(__file__))
WDA_DIR = os.path.join(HERE, "tools", "wechatdataanalysis")
OUT_DIR = os.path.join(HERE, "dist_installer")
MARKER = b"WXCSM_PAYLOAD_V2\n"
XOR = 0xAA

REAL_APP_NAME = "启动工具.app"          # payload 内条目前缀 & 业务 .app 名
WIZARD_BUILD_NAME = "wxcsm-installer"    # PyInstaller --name（ASCII 最稳）
WIZARD_FINAL_NAME = "微信客户沟通总结工具安装向导.app"
REAL_BUILD_NAME = "wxcsm"               # PyInstaller --name（ASCII）
PYINSTALLER = os.environ.get("PYINSTALLER_BIN", "pyinstaller")

UNINSTALL_SH = """#!/bin/bash
# 微信客户沟通总结工具 —— 卸载脚本
echo "正在卸载 微信客户沟通总结工具…"
echo "若 微信客户沟通总结工具.app 正在运行，请先退出。"
read -p "按回车键继续卸载…" _
TARGETS=(
  "$HOME/Applications/微信客户沟通总结工具.app"
  "/Applications/微信客户沟通总结工具.app"
)
for p in "${TARGETS[@]}"; do
  if [ -d "$p" ]; then
    rm -rf "$p" && echo "已删除：$p"
  fi
done
# 删除桌面替身
ALIAS="$HOME/Desktop/微信客户沟通总结工具.app"
if [ -e "$ALIAS" ]; then
  rm -f "$ALIAS" && echo "已删除桌面替身：$ALIAS"
fi
echo "卸载完成。"
read -p "按回车键退出…" _
"""


def _wda_installer_files() -> list:
    if not os.path.isdir(WDA_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(WDA_DIR)):
        p = os.path.join(WDA_DIR, fn)
        if os.path.isfile(p) and fn.lower().endswith((".dmg", ".pkg", ".zip")):
            out.append(p)
    return out


def _run(cmd: list) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, cwd=HERE, check=True)


def _verify_entry(exe_path: str, script_name: str) -> bool:
    """校验 PyInstaller onefile/onedir .app 的入口脚本，避免缓存混淆。"""
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except Exception:
        return True  # 无法校验时放行（不影响构建）
    arch = CArchiveReader(exe_path)
    return script_name in arch.toc


def _build_real_app(include_wda: bool) -> None:
    print("[1/4] PyInstaller 构建 启动工具.app …")
    real_dist = os.path.join(OUT_DIR, "wxcsm_app")
    real_build = os.path.join(OUT_DIR, "build_real")
    cmd = [PYINSTALLER, "app.py",
           "--name", REAL_BUILD_NAME,
           "--windowed",
           "--osx-app-name", "微信客户沟通总结工具",
           "--noconfirm", "--clean",
           "--distpath", real_dist,
           "--workpath", real_build,
           "--specpath", OUT_DIR]
    if include_wda and _wda_installer_files():
        cmd += ["--add-data", f"{WDA_DIR}:tools/wechatdataanalysis"]
    _run(cmd)

    src_app = os.path.join(real_dist, f"{REAL_BUILD_NAME}.app")
    dst_app = os.path.join(real_dist, REAL_APP_NAME)
    if os.path.isdir(dst_app):
        shutil.rmtree(dst_app)
    shutil.move(src_app, dst_app)

    # 注入卸载脚本（顶层），并置可执行位
    un_path = os.path.join(dst_app, "卸载.command")
    with open(un_path, "w", encoding="utf-8") as f:
        f.write(UNINSTALL_SH)
    os.chmod(un_path, 0o755)

    # 校验入口
    exe = os.path.join(dst_app, "Contents", "MacOS", REAL_BUILD_NAME)
    if os.path.isfile(exe) and not _verify_entry(exe, "app"):
        print("[警告] 启动工具.app 入口脚本校验异常，请清理构建缓存后重试。", file=sys.stderr)


def _build_wizard() -> None:
    print("[2/4] PyInstaller 构建安装向导 .app …")
    wiz_dist = os.path.join(OUT_DIR, "wxcsm_wizard")
    wiz_build = os.path.join(OUT_DIR, "build_wizard")
    cmd = [PYINSTALLER, "installer_macos.py",
           "--name", WIZARD_BUILD_NAME,
           "--windowed",
           "--osx-app-name", "微信客户沟通总结工具 安装向导",
           "--noconfirm", "--clean",
           "--distpath", wiz_dist,
           "--workpath", wiz_build,
           "--specpath", OUT_DIR]
    _run(cmd)
    exe = os.path.join(wiz_dist, f"{WIZARD_BUILD_NAME}.app", "Contents", "MacOS", WIZARD_BUILD_NAME)
    if not _verify_entry(exe, "installer_macos"):
        print("[警告] 安装向导 .app 入口脚本校验异常，请清理构建缓存后重试。", file=sys.stderr)


def _pack_payload() -> bytes:
    print("[3/4] 打包 payload（启动工具.app）…")
    real_app = os.path.join(OUT_DIR, "wxcsm_app", REAL_APP_NAME)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for root, _dirs, files in os.walk(real_app):
            for fn in files:
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, OUT_DIR)
                # 统一用正斜杠，安装端按 / 切分还原
                z.write(full, rel.replace(os.sep, "/"))
    data = buf.getvalue()
    print(f"      payload 大小：{len(data)/1024/1024:.1f} MB")
    return data


def _append_payload() -> None:
    payload = _pack_payload()
    wiz_app = os.path.join(OUT_DIR, "wxcsm_wizard", f"{WIZARD_BUILD_NAME}.app")
    exe = os.path.join(wiz_app, "Contents", "MacOS", WIZARD_BUILD_NAME)
    print("[4/4] 拼接 payload 到安装向导可执行尾部…")
    with open(exe, "rb") as f:
        head = f.read()
    encoded = bytes(b ^ XOR for b in payload)
    with open(exe, "wb") as f:
        f.write(head)
        f.write(MARKER)
        f.write(encoded)
    # 重命名向导 .app
    final = os.path.join(OUT_DIR, WIZARD_FINAL_NAME)
    if os.path.isdir(final):
        shutil.rmtree(final)
    shutil.move(wiz_app, final)
    print(f"✅ 完成：{final}  ({os.path.getsize(final)/1024/1024:.1f} MB)")
    print("   发给他人在 Mac 上双击即可运行安装向导（选目录 + 桌面替身）。")


def _make_dmg() -> None:
    final = os.path.join(OUT_DIR, WIZARD_FINAL_NAME)
    dmg = os.path.join(OUT_DIR, "微信客户沟通总结工具安装向导.dmg")
    print("额外：hdiutil 打成 .dmg …")
    if os.path.isfile(dmg):
        os.remove(dmg)
    subprocess.run(["hdiutil", "create", "-volname", "微信客户沟通总结工具",
                    "-srcfolder", final, "-ov", dmg], check=True)
    print(f"✅ DMG：{dmg}")


def main() -> int:
    include_wda = "--no-wda" not in sys.argv
    make_dmg = "--dmg" in sys.argv
    os.makedirs(OUT_DIR, exist_ok=True)
    if include_wda:
        n = len(_wda_installer_files())
        if n == 0:
            print(f"[提示] tools/wechatdataanalysis/ 下未找到 macOS 版 WDA 安装包"
                  f"（.dmg/.pkg/.zip），将构建【不含 WDA】的版本。\n"
                  f"       如需内嵌，请先放入 macOS 版 WeChatDataAnalysis 安装包后重试。")
    _build_real_app(include_wda)
    _build_wizard()
    _append_payload()
    if make_dmg:
        _make_dmg()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
