# macOS 版本说明

本工具为**跨平台**桌面工具（Python + tkinter），提供 macOS 版。GUI、三种数据源、本地/AI 双引擎总结、Excel 导出在 macOS 上均可使用。

> ⚠️ **如实说明**：本 macOS 版在 **Windows 开发机上制作并做静态/导入兼容自检**，未在真实 macOS 微信上端到端验证。请在 Mac 上构建后实际跑一次确认。

## 一、macOS 上支持 / 不支持

| 功能 | macOS | 说明 |
|---|---|---|
| 主界面 GUI | ✅ | tkinter 跨平台 |
| 内置样例数据 demo | ✅ | 免配置试用 |
| **导入聊天记录文件** | ✅ | 跨平台 |
| **读取已解密数据库** | ✅ | 跨平台 |
| 本地总结 / AI 总结 | ✅ | 走 HTTP |
| Excel 导出 | ✅ | openpyxl |
| **直接读取本机微信（wx4mac）** | 🟡 尽力实现 | macOS 版新增 `core/mac_wx4/`：Mach VM 内存扫钥 + SQLCipher4 页解密 + mac 目录定位；GUI 在 mac 上会显示「直接读取本机微信（macOS 4.x）」。**需真机验证**，且需先 `sudo` 重签微信去掉 Hardened Runtime（系统级一次性操作）。详见 `docs/macOS微信直读_真机步骤.md`。 |

**关于「直接读取本机微信」**：Windows 版 `wx4` 数据源依赖 Windows 专属机制（winreg/DPAPI/ReadProcessMemory/Weixin.dll），macOS 无法复用。为此新增 macOS 版 **`wx4mac`** 数据源（`core/mac_wx4/`），按公开资料实现 macOS 微信 4.x 的取钥+解密。**当前为“尽力实现 + 需真机验证”状态**——在装有微信 4.x 的 Mac 上按 `docs/macOS微信直读_真机步骤.md` 跑通后即为正式可用；验证完成前，实际取数仍建议用「导入文件 / 已解密库」数据源。

## 二、在 Mac 上运行（开发调试）

```bash
# 需要带 tkinter 的 python3 (macOS 系统 python3 自带)
python3 -c "import tkinter; print(tkinter.TkVersion)"

cd 项目根
python3 app.py            # 直接跑 GUI
python3 cli.py run --source demo --all --preset 30d --out ~/Desktop/总结.xlsx   # 命令行冒烟
```

## 三、在 Mac 上打包成 .app / .dmg

```bash
pip3 install --user pyinstaller
python3 make_macos.py            # 产出 dist_mac/微信客户沟通总结工具.app
python3 make_macos.py --dmg      # 额外打 .dmg 便于拖拽安装
```

产物：`dist_mac/微信客户沟通总结工具.app`（可选 `.dmg`）。未签名 app 首次打开会被 Gatekeeper 拦，右键 → 打开 放行一次即可。

## 四、用 GitHub Actions 自动构建（无需本机 Mac）

仓库已配置 `.github/workflows/build-macos.yml`：
1. 把项目推到 GitHub 的 main 分支；
2. GitHub 会自动在 macOS runner 上构建；
3. Actions 页的 Artifact `wxcsm-macos` 里下载 `.app` / `.dmg`。

## 五、已知边界

- 不内嵌、不依赖任何第三方工具。
- Windows 版 `wx4` 仅 Windows；macOS 版 `wx4mac` 仅 macOS（Mach VM + Hardened Runtime 重签）。
- macOS 微信直读（`wx4mac`）为**尽力实现、未真机验证**：需在装有微信 4.x 的 Mac 上按
  `docs/macOS微信直读_真机步骤.md` 跑通；验证完成前该数据源可能不可用，请用其余数据源。
