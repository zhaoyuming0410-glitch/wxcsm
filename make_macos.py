#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""macOS 打包脚本 —— 在 macOS(或 GitHub Actions macos-latest)上把 app.py 打成 .app / .dmg。

为何必须在此跑:
  PyInstaller 打 macOS .app 必须在 macOS 上执行(Windows 无法交叉编译 macOS 产物)。
  你可本机 Mac 跑, 或用 GitHub Actions macos runner 自动跑(.github/workflows/build-macos.yml)。

用法(在项目根):
  python3 make_macos.py            # 打 .app (默认 dist_mac/)
  python3 make_macos.py --dmg      # 额外打 .dmg 便于分发

产物:
  dist_mac/微信客户沟通总结工具.app
  dist_mac/微信客户沟通总结工具.dmg (若 --dmg)

说明:
  - 不含任何第三方组件; wx4「直读本机微信」数据源为 Windows 专属, macOS 端不支持该数据源,
    但 demo / 导入文件 / 已解密库 数据源与 总结/AI/Excel 导出 均可用。
  - 本脚本刻意不依赖 PyInstaller 以外的第三方(仅标准库)。
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "微信客户沟通总结工具"
# 反向 DNS 形式的 bundle id(ASCII)。必须显式传给 PyInstaller:
# 否则 PyInstaller 默认拿 --name 当 CFBundleIdentifier, 于是变成中文名,
# 不符合 Apple 规范(会影响 LaunchServices / 偏好存储 / 公证)。
BUNDLE_ID = "com.fenbeitong.wxcsm"
OUT_DIR = os.path.join(HERE, "dist_mac")


def _log(*a):
    print(*a, flush=True)


def _run(args):
    _log("  $", " ".join(str(x) for x in args))
    subprocess.run([str(x) for x in args], cwd=HERE, check=True)


def main():
    make_dmg = "--dmg" in sys.argv
    os.makedirs(OUT_DIR, exist_ok=True)

    _log("[1/3] 用 PyInstaller 构建 .app (--windowed, 无控制台) ...")
    # macOS 上建议用系统 python3(自带 tkinter) 或带 python-tk 的 python; PyInstaller 需已 pip 安装。
    _run([sys.executable, "-m", "PyInstaller",
          "--noconfirm", "--clean",
          "--windowed",
          "--name", APP_NAME,
          "--osx-bundle-identifier", BUNDLE_ID,
          "--distpath", OUT_DIR,
          "--workpath", os.path.join(OUT_DIR, "build"),
          "--specpath", os.path.join(OUT_DIR, "spec"),
          "app.py"])

    app_path = os.path.join(OUT_DIR, f"{APP_NAME}.app")
    if not os.path.isdir(app_path):
        _log("[失败] 未生成 .app, 请确认已 pip install pyinstaller 且 python 带 tkinter。")
        sys.exit(1)
    _log(f"  ✅ .app 已生成: {app_path}")

    # 校验产物里确实含 tkinter, 避免打成"双击闪退/找不到 tk"的假包
    _log("[2/3] 校验产物包含 tkinter ...")
    found_tk = False
    for dp, _dn, fn in os.walk(app_path):
        if any("_tkinter" in d for d in _dn) or \
           any("_tkinter" in f for f in fn):
            found_tk = True
            break
    _log(f"  tkinter 内嵌: {'✅ 是' if found_tk else '⚠️ 未检测到(建议改用带 tkinter 的 python 重打)'}")

    if make_dmg:
        _log("[3/3] 打 .dmg ...")
        dmg = os.path.join(OUT_DIR, f"{APP_NAME}.dmg")
        if os.path.exists(dmg):
            os.remove(dmg)
        # 用 hdiutil 把 .app 包进 dmg; 便于拖拽安装。
        staging = os.path.join(OUT_DIR, "_dmg_staging")
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging, exist_ok=True)
        # copytree 必须显式 symlinks=True。
        # 默认 symlinks=False 会把符号链接"解引用"成实体副本, 于是 Python3.framework 的
        # Versions/Current 等软链被展开成重复目录 —— dmg 里的 .app 结构被破坏且体积虚增。
        shutil.copytree(app_path, os.path.join(staging, os.path.basename(app_path)),
                        symlinks=True)
        # 拖拽安装的落点要做成指向 /Applications 的符号链接。
        # 若建成普通空目录, 用户把 .app 拖进去会尝试写入只读镜像而失败。
        lnk = os.path.join(staging, "Applications")
        try:
            if not os.path.islink(lnk):
                os.symlink("/Applications", lnk)
        except OSError as e:
            _log(f"  ⚠️ 未能创建 Applications 软链: {e}")
        _run(["hdiutil", "create", "-volname", APP_NAME, "-srcfolder", staging,
              "-ov", "-format", "UDZO", dmg])
        shutil.rmtree(staging, ignore_errors=True)
        _log(f"  ✅ .dmg 已生成: {dmg}")

    _log("\n完成。可分发给 mac 用户: 双击 .dmg → 把 .app 拖入 Applications 即可。")
    _log("注意: macOS 首次打开未签名 app 会被 Gatekeeper 拦截, 右键→打开 放行一次即可。")


if __name__ == "__main__":
    main()
