# macOS 版本说明

本工具为**跨平台**桌面工具（Python + tkinter），提供 macOS 版。GUI、三种数据源、本地/AI 双引擎总结、Excel 导出在 macOS 上均可使用。

> ⚠️ **如实说明**：本 macOS 版在 **Windows 开发机上制作并做静态/导入兼容自检**。CI 已在**真实 macOS runner** 上对构建产物做自动化校验（`.app` 结构/符号链接/动态库依赖、`.dmg` 挂载校验、`mac_wx4` 模块自检、启动冒烟），但**没有真实 macOS 微信环境做过端到端取钥解密验证**——这一项必须由你在装有微信的 Mac 上实跑确认。

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

打包后可自行校验产物（建议每次改动后跑一次）：

```bash
python3 verify_macos.py                 # 全量校验 dist_mac/ 下的产物
python3 verify_macos.py --require-dmg   # 要求 .dmg 必须存在(缺则判失败)
python3 verify_macos.py --no-launch     # 跳过启动冒烟
```

校验内容：

| 分组 | 检查项 |
|---|---|
| `.app` 结构 | Info.plist 存在且合法（`plutil -lint`）、`CFBundleExecutable` 与实际二进制一致、`CFBundleIdentifier` 是反向 DNS 的 ASCII 标识符、主可执行文件存在且有 `x` 位、**包内符号链接完好**、`Python3.framework/Versions/Current` 是软链、`_tkinter` 已内嵌 |
| 架构 | `lipo -archs` 与宿主架构兼容（Apple silicon 上需 arm64 或 universal） |
| 依赖 | `otool -L` 主二进制与 `Python3.framework` 二进制，非系统动态库能否在包内找到（缺 dylib = 双击闪退的典型原因） |
| `.dmg` | UDIF 签名（`koly`）、`hdiutil verify`、挂载、**卷根 `Applications` 是否指向 `/Applications` 的符号链接**、镜像内 `.app` 符号链接、卸载 |
| `mac_wx4` 自检 | 真 Darwin 上各子模块可 import、`IS_MAC` 为真、数据源注册表含 `wx4mac`、`detect` 优雅退出、密码学库可用 |
| 启动冒烟 | 拉起 `.app` 观察是否 10 秒内秒退；区分「缺库/缺模块崩溃」（判失败）与「无图形会话」（仅告警） |

> 为什么专门查符号链接：GitHub Actions 的 artifact 是 **zip**，而 **zip 不保留符号链接与权限位**。若把 artifact 里的 `.app` 目录解压到非 macOS 系统，`Python3.framework/Versions/Current` 这类软链会被展开成副本，应用会启动失败。`verify_macos.py` 会把这种情况判为硬失败。
>
> 这套校验已经实际抓到过两个打包缺陷（现均已修复）：打 `.dmg` 时 `shutil.copytree` 漏了 `symlinks=True`，把符号链接解引用展开，导致镜像里的 `.app` 结构被破坏；以及卷根 `Applications` 被建成了实体空目录，用户往只读镜像里拖拽必然失败。

## 四、用 GitHub Actions 自动构建（无需本机 Mac）

仓库已配置 `.github/workflows/build-macos.yml`：
1. 把项目推到 GitHub 的 main 分支（或在 Actions 页手动 Run workflow，可勾选是否打 `.dmg`）；
2. GitHub 自动在 macOS runner 上：装依赖 → import 自检 → 构建 `.app`（可选 `.dmg`）→ demo 数据源冒烟 → **跑 `verify_macos.py` 校验产物** → 打包 `.app.tar.gz`；
3. Actions 页的 Artifact `wxcsm-macos` 里下载。

**下载物怎么选（重要）**：优先用 **`.dmg`** —— 它是自包含磁盘映像，符号链接与权限位都完好，双击即可拖入「应用程序」。若确实需要 `.app` 本体，请用同一 artifact 里的 **`微信客户沟通总结工具.app.tar.gz`**（tar 保留符号链接），**不要**用从 zip 里解出来的 `.app` 目录。

> 校验步骤放在上传产物**之前**：一旦校验不过，本次构建即失败且不会产出 artifact，避免把坏包发出去。

## 五、已知边界

- 不内嵌、不依赖任何第三方工具。
- Windows 版 `wx4` 仅 Windows；macOS 版 `wx4mac` 仅 macOS（Mach VM + Hardened Runtime 重签）。
- macOS 微信直读（`wx4mac`）为**尽力实现、未真机验证**：需在装有微信 4.x 的 Mac 上按
  `docs/macOS微信直读_真机步骤.md` 跑通；验证完成前该数据源可能不可用，请用其余数据源。
- **CI 能验 / 不能验的边界**：CI 在真 macOS 上验的是「包能不能起来、模块能不能 import、注册表对不对」这类**结构性/模块级**问题；它**验不了**「真的从微信里取到密钥并解密出聊天记录」——runner 上没有微信、也没有登录态，那一步只能在真机上做。
- CI 的「启动冒烟」若因 runner 无图形会话而退出，脚本只告警不判失败，避免误报。
