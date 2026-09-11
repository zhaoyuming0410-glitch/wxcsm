#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""macOS 构建产物校验 —— 在真 macOS(或 GitHub Actions macos runner)上运行。

做什么:
  1) 校验 .app 包结构完整性: Info.plist / 可执行文件与权限位 / 符号链接是否还在
     (GitHub artifact 的 zip 经 Windows 解压会丢符号链接与权限位, 这里专门守这道门)
  2) 校验主二进制的动态库依赖能否在包内找到(缺 dylib = 双击闪退的典型原因)
  3) 校验 .dmg: hdiutil verify + 挂载 + 内部 .app 结构 + 卸载
  4) macOS 微信直读模块自检: 在真 Darwin 上 import / 注册表 / detect 调用是否正常
  5) 启动冒烟: 尝试拉起 .app, 观察是否秒退(区分"缺库崩溃"与"环境无显示")

用法(项目根目录):
  python3 verify_macos.py                 # 全量校验 dist_mac/ 下的产物
  python3 verify_macos.py --no-launch     # 跳过启动冒烟
  python3 verify_macos.py --app X.app --dmg X.dmg

退出码: 0 = 全部通过(可有警告); 1 = 有硬失败。
仅依赖标准库 + macOS 自带命令(hdiutil/otool/plutil), 不装第三方。
"""
from __future__ import annotations

import argparse
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

APP_NAME = "微信客户沟通总结工具"
HERE = Path(__file__).resolve().parent

_results: list[tuple[str, str, str]] = []  # (level, name, detail)


def ok(name: str, detail: str = "") -> None:
    _results.append(("OK", name, detail))
    print(f"  [OK]   {name}" + (f" — {detail}" if detail else ""), flush=True)


def warn(name: str, detail: str = "") -> None:
    _results.append(("WARN", name, detail))
    print(f"  [WARN] {name}" + (f" — {detail}" if detail else ""), flush=True)


def fail(name: str, detail: str = "") -> None:
    _results.append(("FAIL", name, detail))
    print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""), flush=True)


def run(cmd: list[str], timeout: int = 120) -> tuple[int, str]:
    """执行命令, 返回 (退出码, 合并输出)。命令不存在返回 (127, ...)。"""
    if shutil.which(cmd[0]) is None and not Path(cmd[0]).exists():
        return 127, f"命令不存在: {cmd[0]}"
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"超时({timeout}s): {' '.join(cmd)}"
    except Exception as e:  # noqa: BLE001
        return 1, f"执行异常: {e}"


def walk_entries(root: Path):
    """遍历 root, 产出 (路径, 是否符号链接, 是否目录)。不跟随符号链接。"""
    for dp, dns, fns in os.walk(root, followlinks=False):
        base = Path(dp)
        for n in list(dns) + list(fns):
            p = base / n
            try:
                islink = p.is_symlink()
                isdir = p.is_dir() and not islink
            except OSError:
                continue
            yield p, islink, isdir


def count_symlinks(root: Path) -> list[Path]:
    return [p for p, islink, _ in walk_entries(root) if islink]


def deps_of(binary: Path) -> list[str]:
    """otool -L 列出 binary 依赖的库路径(去掉版本后缀)。"""
    rc, out = run(["otool", "-L", str(binary)])
    if rc != 0:
        return []
    deps = []
    for ln in out.splitlines()[1:]:
        ln = ln.strip()
        if not ln:
            continue
        dep = ln.split(" (compatibility")[0].strip()
        if dep and not dep.startswith("/usr/lib/") and not dep.startswith("/System/"):
            deps.append(dep)
    return deps


# ---------------------------------------------------------------- .app 校验

def verify_app(app: Path) -> None:
    print(f"\n[1] 校验 .app 包结构: {app}", flush=True)
    if not app.exists():
        fail(".app 存在", str(app))
        return
    ok(".app 存在", str(app))
    if not app.is_dir():
        fail(".app 是目录", "不是目录")
        return

    # Info.plist
    plist = app / "Contents" / "Info.plist"
    pl = None
    if plist.is_file():
        ok("Info.plist 存在")
        if sys.platform == "darwin":
            rc, out = run(["plutil", "-lint", str(plist)])
            if rc == 0:
                ok("Info.plist 格式合法", out.strip().splitlines()[-1] if out.strip() else "")
            else:
                fail("Info.plist 格式非法", out.strip()[:200])
        try:
            with open(plist, "rb") as f:
                pl = plistlib.load(f)
        except Exception as e:  # noqa: BLE001
            warn("Info.plist 可解析", str(e)[:120])
    else:
        fail("Info.plist 存在", "缺失")

    # 主可执行文件
    macos_dir = app / "Contents" / "MacOS"
    exe = macos_dir / APP_NAME
    if not exe.is_file():
        cands = [p for p in (macos_dir.iterdir() if macos_dir.is_dir() else []) if p.is_file()]
        if cands:
            exe = cands[0]
            warn("主可执行文件名与预期不同", f"预期 {APP_NAME}, 实得 {exe.name}")
        else:
            fail("主可执行文件存在", f"{macos_dir} 下无可执行文件")
            return
    ok("主可执行文件存在", exe.name)
    if os.access(exe, os.X_OK):
        ok("主可执行文件有执行权限")
    else:
        fail("主可执行文件有执行权限", "无 x 位 —— 解压/传输过程丢了权限位")

    # Info.plist 关键键与实际产物是否一致
    if pl:
        exe_decl = pl.get("CFBundleExecutable")
        if exe_decl == exe.name:
            ok("CFBundleExecutable 与实际二进制一致", exe_decl)
        elif exe_decl:
            fail("CFBundleExecutable 与实际二进制一致", f"声明 {exe_decl}, 实为 {exe.name}")
        else:
            fail("CFBundleExecutable 已设置", "缺失 —— 双击可能无法启动")
        bid = pl.get("CFBundleIdentifier") or ""
        if not bid:
            fail("CFBundleIdentifier 已设置", "缺失 —— 双击可能无法启动")
        elif "yourcompany" in bid.lower() or "example" in bid.lower():
            fail("CFBundleIdentifier 已设置", f"占位值: {bid!r}")
        elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", bid) or "." not in bid:
            fail("CFBundleIdentifier 格式为反向 DNS(ASCII)",
                 f"{bid!r} —— 应为 com.example.app 形式。"
                 "中文/非 ASCII 标识符不符合 Apple 规范(给 PyInstaller 传 --osx-bundle-identifier)")
        else:
            ok("CFBundleIdentifier 为反向 DNS 标识符", bid)
        if pl.get("LSMinimumSystemVersion"):
            ok("LSMinimumSystemVersion 已声明", str(pl["LSMinimumSystemVersion"]))

    # 架构: 必须与 runner 架构兼容(Apple silicon 上需 arm64 或 universal)
    if sys.platform == "darwin":
        rc, out = run(["lipo", "-archs", str(exe)])
        archs = out.strip().split() if rc == 0 and out.strip() else []
        host = os.uname().machine
        if archs:
            if host in archs:
                ok("二进制架构与宿主兼容", f"{' '.join(archs)} (host {host})")
            elif "x86_64" in archs and host == "arm64":
                warn("二进制架构与宿主兼容", f"仅 {' '.join(archs)}, 需 Rosetta 才能跑")
            else:
                fail("二进制架构与宿主兼容", f"{' '.join(archs)} 不含 host {host}")
        else:
            warn("二进制架构", "lipo 未能识别")

    # 符号链接(关键: Windows 解压会丢)
    links = count_symlinks(app)
    if links:
        ok("包内符号链接完好", f"共 {len(links)} 个, 如 {links[0].relative_to(app)}")
    else:
        fail("包内符号链接完好", "一个都没有 —— 典型是 zip 经非 macOS 系统解压丢了链接, "
                                 "这会破坏 Python3.framework 导致无法启动")

    # Python3.framework 的 Versions/Current 软链(最典型的一个)
    fw = app / "Contents" / "Frameworks" / "Python3.framework"
    if fw.is_dir():
        cur = fw / "Versions" / "Current"
        if cur.is_symlink():
            ok("Python3.framework/Versions/Current 是符号链接")
        elif cur.exists():
            warn("Versions/Current 存在但不是符号链接", "可能已被展开为目录副本")
        else:
            fail("Python3.framework/Versions/Current 存在", "缺失")

    # tkinter 内嵌
    has_tk = any("_tkinter" in p.name for p, _, _ in walk_entries(app))
    if has_tk:
        ok("_tkinter 已内嵌")
    else:
        fail("_tkinter 已内嵌", "包内找不到 _tkinter —— 启动会报找不到 tk")

    # 动态库依赖能否在包内解析
    # 主二进制(PyInstaller bootloader)通常只链系统库, 真正要看的是 Python 框架二进制。
    if sys.platform == "darwin":
        targets = [exe]
        vers = fw / "Versions"
        if vers.is_dir():
            for v in vers.iterdir():
                cand = v / "Python3"
                if cand.is_file():
                    targets.append(cand)
        bundle_names = {p.name for p, _, _ in walk_entries(app)}
        checked = 0
        missing: list[str] = []
        for t in targets:
            for d in deps_of(t):
                checked += 1
                base = os.path.basename(d)
                if base and base not in bundle_names:
                    missing.append(f"{t.name} -> {d}")
        if missing:
            fail("非系统动态库依赖均可解析", "包内找不到: " + "; ".join(missing[:4]))
        elif checked == 0:
            ok("非系统动态库依赖均可解析", "已检查的对象只依赖系统库")
        else:
            ok("非系统动态库依赖均可解析", f"共 {checked} 项均能在包内找到")
    else:
        warn("动态库依赖检查", "非 macOS, 跳过 otool")


# ---------------------------------------------------------------- .dmg 校验

def verify_dmg(dmg: Path, app_name: str, require: bool = False) -> None:
    print(f"\n[2] 校验 .dmg: {dmg}", flush=True)
    if not dmg.is_file():
        if require:
            fail(".dmg 存在", str(dmg))
        else:
            warn(".dmg 存在", "未产出(本次未要求打 dmg), 跳过 dmg 校验")
        return
    ok(".dmg 存在", f"{dmg.stat().st_size / 1024 / 1024:.2f} MB")

    # UDIF 尾部签名
    with open(dmg, "rb") as f:
        f.seek(-512, os.SEEK_END)
        trailer = f.read(4)
    if trailer == b"koly":
        ok(".dmg 是有效 UDIF 镜像", "trailer=koly")
    else:
        fail(".dmg 是有效 UDIF 镜像", f"trailer={trailer!r} (期望 b'koly')")

    if sys.platform != "darwin":
        warn(".dmg 挂载校验", "非 macOS, 跳过 hdiutil")
        return

    rc, out = run(["hdiutil", "verify", str(dmg)], timeout=300)
    if rc == 0:
        ok("hdiutil verify 通过")
    else:
        tail = "\n".join(out.strip().splitlines()[-3:])
        fail("hdiutil verify 通过", tail[:300])

    # 挂载
    rc, out = run(["hdiutil", "attach", str(dmg), "-nobrowse", "-readonly", "-noverify",
                   "-mountpoint", "/tmp/wxcsm_dmg_mnt"], timeout=300)
    if rc != 0:
        run(["mkdir", "-p", "/tmp/wxcsm_dmg_mnt"])
        rc, out = run(["hdiutil", "attach", str(dmg), "-nobrowse", "-readonly", "-noverify",
                       "-mountpoint", "/tmp/wxcsm_dmg_mnt"], timeout=300)
    if rc != 0:
        fail("挂载 .dmg", out.strip()[:200])
        return
    ok("挂载 .dmg 成功")
    mnt = Path("/tmp/wxcsm_dmg_mnt")
    try:
        # 拖拽安装落点: 卷根下的 Applications 应是符号链接(/Applications), 而非实体空目录
        alink = mnt / "Applications"
        if alink.is_symlink():
            ok("卷根 Applications 是指向 /Applications 的符号链接", os.readlink(alink))
        elif alink.exists():
            fail("卷根 Applications 是符号链接",
                 "是实体目录 —— 用户往只读镜像里拖 .app 会失败(应 os.symlink('/Applications'))")
        else:
            warn("卷根 Applications", "不存在(不影响使用, 但不能直接拖拽安装)")

        apps = [p for p in mnt.iterdir() if p.suffix == ".app"]
        if apps:
            inner = apps[0]
            ok("挂载卷内含 .app", inner.name)
            ilinks = count_symlinks(inner)
            if ilinks:
                ok("镜像内 .app 符号链接完好", f"{len(ilinks)} 个")
            else:
                fail("镜像内 .app 符号链接完好",
                     "一个都没有 —— 打包时符号链接被解引用展开(检查 make_macos.py 的 "
                     "copytree 是否漏了 symlinks=True)")
            inner_exe = inner / "Contents" / "MacOS" / inner.stem
            if inner_exe.is_file() and os.access(inner_exe, os.X_OK):
                ok("镜像内 .app 主可执行文件可执行")
            elif inner_exe.is_file():
                fail("镜像内 .app 主可执行文件可执行", "存在但无 x 位")
            else:
                warn("镜像内 .app 主可执行文件", f"未找到 {inner_exe.name}")
        else:
            fail("挂载卷内含 .app", f"卷内条目: {[p.name for p in mnt.iterdir()]}")
    finally:
        rc, out = run(["hdiutil", "detach", str(mnt), "-force"], timeout=120)
        if rc == 0:
            ok("卸载 .dmg 成功")
        else:
            warn("卸载 .dmg", out.strip()[:120])


# ------------------------------------------------- mac_wx4 模块自检(真 Darwin)

def verify_mac_wx4() -> None:
    print("\n[3] macOS 微信直读模块自检(真 Darwin)", flush=True)
    if sys.platform != "darwin":
        warn("mac_wx4 自检", f"当前非 macOS({sys.platform}), 跳过 —— 该模块本就只在 macOS 生效")
        return

    # 3.1 平台判定与模块 import
    code = (
        "import sys; sys.path.insert(0,'.');"
        "import core.mac_wx4 as m;"
        "from core.mac_wx4 import locate, decrypt, memkey, reader, data_source;"
        "print('IS_MAC=', m.IS_MAC);"
        "print('has_scan=', hasattr(memkey,'scan_wechat_keys'));"
        "print('has_decrypt=', hasattr(decrypt,'decrypt_db_file'));"
        "print('src_key=', data_source.MacWxLiveSource.key)"
    )
    rc, out = run([sys.executable, "-c", code])
    if rc == 0:
        ok("mac_wx4 各子模块可 import", out.strip().replace("\n", "; "))
        if "IS_MAC= True" in out:
            ok("IS_MAC 在 Darwin 上为 True")
        else:
            fail("IS_MAC 在 Darwin 上为 True", out.strip()[:150])
        if "src_key= wx4mac" in out:
            ok("数据源 key 为 wx4mac")
        else:
            fail("数据源 key 为 wx4mac", out.strip()[:150])
    else:
        fail("mac_wx4 各子模块可 import", out.strip()[-400:])

    # 3.2 注册表: macOS 上应出现 wx4mac
    rc, out = run([sys.executable, "cli.py", "list-sources"])
    if rc == 0 and "wx4mac" in out:
        ok("数据源注册表含 wx4mac(macOS 专属)")
    elif rc == 0:
        fail("数据源注册表含 wx4mac", f"未出现。输出: {out.strip()[:200]}")
    else:
        fail("cli.py list-sources 可运行", out.strip()[-300:])

    # 3.3 detect 应优雅运行(无微信数据时报告"未找到"且退出码 0, 而非崩溃)
    rc, out = run([sys.executable, "-m", "core.mac_wx4", "detect"])
    if rc == 0:
        ok("core.mac_wx4 detect 正常退出", out.strip().splitlines()[0][:120] if out.strip() else "")
    else:
        fail("core.mac_wx4 detect 正常退出", f"rc={rc}; {out.strip()[-300:]}")

    # 3.4 解密所需的密码学库可用(两条兜底路径至少一条通)
    code2 = (
        "ok=False\n"
        "try:\n"
        "    from Crypto.Cipher import AES; ok='pycryptodome'\n"
        "except Exception:\n"
        "    try:\n"
        "        from cryptography.hazmat.primitives.ciphers import Cipher; ok='cryptography'\n"
        "    except Exception:\n"
        "        ok=False\n"
        "print('crypto_backend=', ok)"
    )
    rc, out = run([sys.executable, "-c", code2])
    if "crypto_backend= False" not in out and rc == 0:
        ok("解密用密码学库可用", out.strip())
    else:
        fail("解密用密码学库可用", "pycryptodome 与 cryptography 都不可用")


# ---------------------------------------------------------------- 启动冒烟

def verify_launch(app: Path) -> None:
    print("\n[4] 启动冒烟(拉起 .app 观察是否秒退)", flush=True)
    macos_dir = app / "Contents" / "MacOS"
    if not macos_dir.is_dir():
        fail("启动冒烟", "找不到 Contents/MacOS")
        return
    exes = [p for p in macos_dir.iterdir() if p.is_file()]
    if not exes:
        fail("启动冒烟", "无可执行文件")
        return
    exe = exes[0]
    if sys.platform != "darwin":
        warn("启动冒烟", "非 macOS, 跳过")
        return

    log = Path("/tmp/wxcsm_launch.log")
    try:
        with open(log, "wb") as f:
            proc = subprocess.Popen([str(exe)], stdout=f, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL)
    except Exception as e:  # noqa: BLE001
        fail("启动冒烟", f"无法拉起进程: {e}")
        return

    time.sleep(10)
    alive = proc.poll() is None
    text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""

    if alive:
        ok("进程启动后存活 10s", "未秒退")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        ok("可正常结束进程")
        return

    rc = proc.returncode
    hard = re.search(r"Library not loaded|image not found|no module named|"
                     r"ModuleNotFoundError|ImportError|_tkinter|Tcl|Tk", text)
    if hard:
        fail("启动冒烟", f"进程退出(rc={rc}) 且报缺库/缺模块: {text.strip()[-300:]}")
    elif rc not in (0, None):
        warn("启动冒烟", f"进程退出 rc={rc}(可能是无图形会话导致, 非必然缺陷)。输出: {text.strip()[-200:]}")
    else:
        warn("启动冒烟", f"进程正常退出 rc={rc}, 输出: {text.strip()[-200:]}")


# ---------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default=None, help=".app 路径(默认 dist_mac/<APP_NAME>.app)")
    ap.add_argument("--dmg", default=None, help=".dmg 路径(默认 dist_mac/<APP_NAME>.dmg)")
    ap.add_argument("--no-launch", action="store_true", help="跳过启动冒烟")
    ap.add_argument("--require-dmg", action="store_true",
                    help="要求 .dmg 必须存在(未产出则判为失败)")
    args = ap.parse_args()

    dist = HERE / "dist_mac"
    app = Path(args.app) if args.app else dist / f"{APP_NAME}.app"
    dmg = Path(args.dmg) if args.dmg else dist / f"{APP_NAME}.dmg"

    print(f"平台: {sys.platform} | 校验目录: {dist}", flush=True)
    verify_app(app)
    verify_dmg(dmg, APP_NAME, require=args.require_dmg)
    verify_mac_wx4()
    if args.no_launch:
        warn("启动冒烟", "按 --no-launch 跳过")
    else:
        verify_launch(app)

    n_ok = sum(1 for lv, _, _ in _results if lv == "OK")
    n_warn = sum(1 for lv, _, _ in _results if lv == "WARN")
    n_fail = sum(1 for lv, _, _ in _results if lv == "FAIL")
    print(f"\n===== 校验汇总: OK={n_ok} WARN={n_warn} FAIL={n_fail} =====", flush=True)
    if n_fail:
        print("硬失败项:", flush=True)
        for lv, name, detail in _results:
            if lv == "FAIL":
                print(f"  - {name}: {detail}", flush=True)
        return 1
    print("全部硬性检查通过。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
