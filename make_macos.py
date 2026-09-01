"""生成 macOS 可分发安装包（本脚本只能在 macOS 上运行）。

流程：
  1) PyInstaller 把 app.py 打成 启动工具.app（--windowed），并把
     tools/wechatdataanalysis/ 下的 macOS 版 WeChatDataAnalysis 安装包
     （.dmg / .pkg / .zip）通过 --add-data 打进
     启动工具.app/Contents/Resources/wechatdataanalysis/（wda_launcher 会自动找到）。
  2) 注入「卸载.command」到 启动工具.app 顶层（双击即可卸载）。
  3) PyInstaller 把 installer_macos.py 打成安装向导 .app（--windowed）。
  4) 把 启动工具.app 递归打成 payload.zip（ZIP_STORED），XOR(0xAA) 后写入
     安装向导 .app 的 Contents/Resources/wxcsm_payload.bin（独立文件，不碰 Mach-O）。
     —— 放在 Resources 而非拼到可执行尾部，是为了不破坏向导 .app 的 ad-hoc codesign，
        否则他人 Mac 会因「签名严格校验失败」硬拒打开（同 Windows 版用 XOR 防 cookie 误识别）。
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
import plistlib
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


def _patch_display(app_path: str, display_name: str) -> None:
    """把 .app 在 Finder 中显示的中文名写入 Info.plist（CFBundleName/CFBundleDisplayName）。"""
    ip = os.path.join(app_path, "Contents", "Info.plist")
    if not os.path.isfile(ip):
        return
    try:
        with open(ip, "rb") as f:
            pl = plistlib.load(f)
        pl["CFBundleName"] = display_name
        pl["CFBundleDisplayName"] = display_name
        with open(ip, "wb") as f:
            plistlib.dump(pl, f)
        print(f"      Info.plist 显示名 -> {display_name}")
    except Exception as e:
        print(f"[警告] 写入 Info.plist 失败（不影响构建）：{e}", file=sys.stderr)


def _codesign(app_path: str) -> None:
    """对 .app 做 ad-hoc 重签名；若失败则清掉签名，避免「无效签名」被硬拒。"""
    try:
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-", app_path],
                       cwd=HERE, check=True)
        print("      codesign (ad-hoc) 完成")
    except Exception as e:
        print(f"[警告] codesign 失败：{e}", file=sys.stderr)
        # 失败时不要留「无效签名」状态：清掉签名，至少能靠用户手动放行打开
        try:
            subprocess.run(["codesign", "--remove-signature", app_path],
                           cwd=HERE, check=True)
            print("      已移除签名（他人 Mac 需手动放行后打开）。", file=sys.stderr)
        except Exception:
            pass


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

    # 设置 Finder 中文显示名并重签名（避免改 plist 破坏签名）
    _patch_display(dst_app, "微信客户沟通总结工具")
    _codesign(dst_app)

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
           "--noconfirm", "--clean",
           "--distpath", wiz_dist,
           "--workpath", wiz_build,
           "--specpath", OUT_DIR]
    _run(cmd)
    wiz_app = os.path.join(wiz_dist, f"{WIZARD_BUILD_NAME}.app")
    # 设置 Finder 中文显示名（此时 payload 尚未写入，先改 plist；签名在写 payload 后统一做）
    _patch_display(wiz_app, "微信客户沟通总结工具 安装向导")
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
                # 以 .app 根为起点（Contents/...、卸载.command），安装端直接落到目标 .app 下，
                # 不要带 wxcsm_app/ 前缀，否则安装后的 .app 会被错误地嵌套一层。
                rel = os.path.relpath(full, real_app).replace(os.sep, "/")
                z.write(full, rel)
    data = buf.getvalue()
    print(f"      payload 大小：{len(data)/1024/1024:.1f} MB")
    return data


def _write_payload() -> None:
    payload = _pack_payload()
    wiz_app = os.path.join(OUT_DIR, "wxcsm_wizard", f"{WIZARD_BUILD_NAME}.app")
    res_dir = os.path.join(wiz_app, "Contents", "Resources")
    os.makedirs(res_dir, exist_ok=True)
    bin_path = os.path.join(res_dir, "wxcsm_payload.bin")
    print("[4/4] 写入 payload 到 Contents/Resources/wxcsm_payload.bin …")
    encoded = bytes(b ^ XOR for b in payload)
    with open(bin_path, "wb") as f:
        f.write(encoded)
    # Mach-O 未被改动（payload 在 Resources），做一次 ad-hoc 重签名保证一致
    _codesign(wiz_app)
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
    _write_payload()
    if make_dmg:
        _make_dmg()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
