"""WeChatDataAnalysis 启动器逻辑测试（mock 子进程，不真正拉起任何程序）。

覆盖 detect_status / launch_wda 的三种分支：
  installed   -> 直接启动已装 exe
  not_installed -> 打开安装包
  missing     -> 无安装包
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.wda_launcher as wda


class TestWdaLauncher(unittest.TestCase):

    def test_tools_root_resolves(self):
        # 本机 tools/wechatdataanalysis 存在，应能解析出正确根
        root = wda.tools_root()
        self.assertTrue(os.path.isdir(os.path.join(root, "wechatdataanalysis")),
                        f"tools_root 应命中含 wechatdataanalysis 的目录，实际: {root}")

    def test_detect_returns_valid_state(self):
        # 不假设本机装没装，只需返回合法三态之一；若未装则应能找到安装包
        st, path = wda.detect_status()
        self.assertIn(st, ("installed", "not_installed", "missing"))
        if st == "installed":
            self.assertTrue(path.lower().endswith(".exe"), path)
        elif st == "not_installed":
            self.assertTrue(path.lower().endswith(".exe"), path)

    def test_launch_installed(self):
        with mock.patch.object(wda, "find_wda_exe", return_value=r"C:\Apps\WeChatDataAnalysis\WeChatDataAnalysis.exe"), \
             mock.patch.object(wda, "find_installer", return_value=None), \
             mock.patch.object(wda.subprocess, "Popen", autospec=True) as m_pop:
            res = wda.launch_wda()
            self.assertEqual(res["action"], "launched")
            self.assertEqual(res["name"], "WeChatDataAnalysis.exe")
            m_pop.assert_called_once()
            args, kwargs = m_pop.call_args
            self.assertEqual(args[0], [r"C:\Apps\WeChatDataAnalysis\WeChatDataAnalysis.exe"])
            self.assertEqual(kwargs.get("cwd"), r"C:\Apps\WeChatDataAnalysis")

    def test_launch_installer(self):
        installer = r"D:\wxcsm\tools\wechatdataanalysis\WeChatDataAnalysis-2.3.0-Setup.exe"
        with mock.patch.object(wda, "find_wda_exe", return_value=None), \
             mock.patch.object(wda, "find_installer", return_value=installer), \
             mock.patch.object(wda.subprocess, "Popen", autospec=True) as m_pop:
            res = wda.launch_wda()
            self.assertEqual(res["action"], "installer")
            self.assertEqual(res["path"], installer)
            m_pop.assert_called_once()

    def test_launch_no_installer(self):
        with mock.patch.object(wda, "find_wda_exe", return_value=None), \
             mock.patch.object(wda, "find_installer", return_value=None), \
             mock.patch.object(wda.subprocess, "Popen", autospec=True) as m_pop:
            res = wda.launch_wda()
            self.assertEqual(res["action"], "no_installer")
            m_pop.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestWdaLauncherMacOS(unittest.TestCase):
    """macOS 分支（mock sys.platform=darwin）：探测 .app、识别 .dmg/.pkg/.zip、
    启动统一用 `open` 命令。"""

    _MAC = dict(IS_MAC=True, IS_WIN=False)

    def test_mac_find_installer_dmg_pkg(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            pkg = os.path.join(td, "wechatdataanalysis")
            os.makedirs(pkg)
            open(os.path.join(pkg, "WeChatDataAnalysis-2.3.0.dmg"), "w").close()
            open(os.path.join(pkg, "WeChatDataAnalysis-2.3.0.pkg"), "w").close()
            with mock.patch.multiple(wda, **self._MAC), \
                 mock.patch.object(wda, "_wda_pkg_dir", return_value=pkg):
                hit = wda.find_installer()
                self.assertIsNotNone(hit)
                self.assertTrue(hit.lower().endswith((".dmg", ".pkg", ".zip")), hit)

    def test_mac_find_wda_exe(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            app = os.path.join(td, "WeChatDataAnalysis.app")
            os.makedirs(os.path.join(app, "Contents", "MacOS"))
            with mock.patch.multiple(wda, **self._MAC), \
                 mock.patch.object(wda, "_mac_installed_candidates", return_value=[app]):
                self.assertEqual(wda.find_wda_exe(), app)

    def test_mac_find_wda_exe_missing(self):
        with mock.patch.multiple(wda, **self._MAC), \
             mock.patch.object(wda, "_mac_installed_candidates", return_value=[]):
            self.assertIsNone(wda.find_wda_exe())

    def test_mac_detect_installed(self):
        with mock.patch.multiple(wda, **self._MAC), \
             mock.patch.object(wda, "find_wda_exe", return_value="/Applications/WeChatDataAnalysis.app"), \
             mock.patch.object(wda, "find_installer", return_value=None):
            st, path = wda.detect_status()
            self.assertEqual(st, "installed")
            self.assertTrue(path.endswith(".app"), path)

    def test_mac_detect_not_installed(self):
        with mock.patch.multiple(wda, **self._MAC), \
             mock.patch.object(wda, "find_wda_exe", return_value=None), \
             mock.patch.object(wda, "find_installer", return_value="/tmp/WeChatDataAnalysis.dmg"):
            st, path = wda.detect_status()
            self.assertEqual(st, "not_installed")
            self.assertTrue(path.endswith(".dmg"), path)

    def test_mac_detect_missing(self):
        with mock.patch.multiple(wda, **self._MAC), \
             mock.patch.object(wda, "find_wda_exe", return_value=None), \
             mock.patch.object(wda, "find_installer", return_value=None):
            self.assertEqual(wda.detect_status(), ("missing", None))

    def test_mac_launch_open_app(self):
        with mock.patch.multiple(wda, **self._MAC), \
             mock.patch.object(wda, "find_wda_exe", return_value="/Applications/WeChatDataAnalysis.app"), \
             mock.patch.object(wda, "find_installer", return_value=None), \
             mock.patch.object(wda.subprocess, "Popen", autospec=True) as m_pop:
            res = wda.launch_wda()
            self.assertEqual(res["action"], "launched")
            m_pop.assert_called_once()
            args, _ = m_pop.call_args
            self.assertEqual(args[0], ["open", "/Applications/WeChatDataAnalysis.app"])

    def test_mac_launch_open_installer(self):
        inst = "/tmp/WeChatDataAnalysis-2.3.0.dmg"
        with mock.patch.multiple(wda, **self._MAC), \
             mock.patch.object(wda, "find_wda_exe", return_value=None), \
             mock.patch.object(wda, "find_installer", return_value=inst), \
             mock.patch.object(wda.subprocess, "Popen", autospec=True) as m_pop:
            res = wda.launch_wda()
            self.assertEqual(res["action"], "installer")
            m_pop.assert_called_once()
            args, _ = m_pop.call_args
            self.assertEqual(args[0], ["open", inst])

    def test_mac_installer_hint(self):
        with mock.patch.multiple(wda, **self._MAC):
            self.assertIn(".dmg", wda.installer_hint())
            self.assertIn(".pkg", wda.installer_hint())
