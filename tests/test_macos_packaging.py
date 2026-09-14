# -*- coding: utf-8 -*-
"""macOS 打包脚本的自洽测试。

只测能在非 macOS 上验的部分：Info.plist 的版本写入（`plistlib` 是跨平台的）。
真正的 `.app` / `.dmg` 构建必须在 macOS 上跑，见 make_macos.py 开头说明。

为什么专门测这一条：PyInstaller 除了 `--osx-bundle-identifier` 之外没有传元数据的
命令行开关，默认把 `CFBundleShortVersionString` 写成 `0.0.0`。于是 Finder「显示简介」
里版本是 0.0.0，而窗口标题写着 v2.2.0——用户想确认"我装的是不是新版"时对不上号
（Windows 版就吃过这个亏：版本号一直没变，有人对着 6 天前的旧包找了半天新功能）。
"""

from __future__ import annotations

import plistlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import make_macos                                                # noqa: E402
from core import __version__                                     # noqa: E402


def _fake_app(root: Path, plist: dict | None = None) -> Path:
    """造一个只有 Info.plist 的假 .app（不需要真在 macOS 上）。"""
    app = root / "某App.app"
    (app / "Contents").mkdir(parents=True)
    with open(app / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump(plist or {
            "CFBundleExecutable": "某App",
            "CFBundleIdentifier": "com.lvpaopao.wxcsm",
            "CFBundleShortVersionString": "0.0.0",
            "CFBundleVersion": "0.0.0",
            "LSMinimumSystemVersion": "10.13",
        }, f)
    return app


def _read(app: Path) -> dict:
    with open(app / "Contents" / "Info.plist", "rb") as f:
        return plistlib.load(f)


class TestBundleVersion(unittest.TestCase):
    def test_仓库版本号可读(self):
        """read_version 必须和 core.__version__ 是同一个来源。"""
        self.assertRegex(__version__, r"^\d+\.\d+")
        self.assertEqual(make_macos.read_version(), __version__)

    def test_写入版本且不丢其它键(self):
        with tempfile.TemporaryDirectory() as d:
            app = _fake_app(Path(d))
            pl = _read(app)
            self.assertEqual(pl["CFBundleShortVersionString"], "0.0.0",
                             "前置：PyInstaller 的默认值就是 0.0.0")

            got = make_macos.set_bundle_version(str(app), "9.9.9")
            self.assertEqual(got, "9.9.9")
            pl = _read(app)
            self.assertEqual(pl["CFBundleShortVersionString"], "9.9.9")
            self.assertEqual(pl["CFBundleVersion"], "9.9.9")
            # 其它键一个都不能丢：少了 CFBundleIdentifier / CFBundleExecutable
            # 这个 .app 直接起不来，比版本号显示错更严重。
            self.assertEqual(pl["CFBundleIdentifier"], "com.lvpaopao.wxcsm")
            self.assertEqual(pl["CFBundleExecutable"], "某App")
            self.assertEqual(pl["LSMinimumSystemVersion"], "10.13")

    def test_写出来仍是合法_XML_plist(self):
        with tempfile.TemporaryDirectory() as d:
            app = _fake_app(Path(d))
            make_macos.set_bundle_version(str(app), "2.2.0")
            raw = (app / "Contents" / "Info.plist").read_bytes()
            self.assertIn(b"<?xml", raw, "应当是 XML plist（macOS 与 plutil 都读这个）")
            self.assertEqual(plistlib.loads(raw)["CFBundleVersion"], "2.2.0")

    def test_缺少_Info_plist_要报错而不是静默跳过(self):
        """静默跳过的话，CI 会"构建成功"却发出一个版本号是 0.0.0 的包。"""
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError):
                make_macos.set_bundle_version(str(Path(d) / "不存在.app"), "1.0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
