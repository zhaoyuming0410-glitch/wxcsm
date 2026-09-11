# 微信客户沟通记录提取与总结工具

面向客户成功团队的沟通归档工具。选客户 → 选时间 → 一键生成沟通总结 → 存进 Excel。
全程图形界面操作，不需要任何编程或命令行基础。

---

## 一、最快上手（3 步）

1. 双击 **`启动工具.exe`**（真正的单文件可执行程序，无需安装 Python，双击即用；若没有 exe 或想用 Python 启动，可双击 `启动工具.bat`）
2. 数据来源保持默认的 **「内置样例数据」**，点「加载聊天对象」→ 勾几个 → 点「开始提取并生成总结」
3. 看一眼弹出的 Excel，确认产出格式符合你的归档要求

样例数据跑通后，再切到真实数据源（见第三节）。

---

## 二、界面六步

| 步骤 | 做什么 | 说明 |
|---|---|---|
| ① | 选择聊天记录来源 | 四种方式，见第三节 |
| ② | 勾选聊天对象 | 支持搜索 + 多选。**搜索不会清空已勾选的对象**，可以「搜A勾上→搜B再勾上→一起生成」 |
| ③ | 设置时间范围 | 点「近 7 天」「本月」等按钮最省事，也可手填 `2026-08-01` |
| ④ | 选总结生成方式 | 默认「智能」：配了 AI 密钥就用 AI，没配自动改用本地总结 |
| ⑤ | 设置 Excel 保存位置 | 建议用「追加」模式，长期累积到同一个台账文件 |
| ⑥ | 查看与修订结果 | 双击任意一行可改文字，改完点「导出到 Excel」再存一次 |

**每一个「聊天对象 + 时间范围」产出恰好一篇 50–200 字的独立总结，对应 Excel 的一行。**

### Excel 输出格式（固定三列，不多不少）

| 客户名称 | 时间范围 | 总结内容 |
|---|---|---|
| 张伟 · 恒瑞医药 | 2026-08-02 ~ 2026-08-31 | 期间共沟通 12 条消息，核心围绕审批流配置…… |

---

## 三、数据来源：请务必读这一节

微信的聊天记录存在本机一个**加密数据库**里，密钥在微信进程内存中。
本工具支持四种数据源，**上层功能完全一致，随时可切换**：

| 数据源 | 适用场景 | 稳定性 | 需要准备什么 |
|---|---|---|---|
| **内置样例数据** | 试用、培训、验收产出格式 | ★★★★★ | 无 |
| **本机微信 4.x 加密库直读**（Windows） | 日常生产使用（Windows 微信 4.x） | ★★★★☆ | 微信保持登录状态，首次使用需自动抓取数据库密钥（只读内存扫描，无注入、无重启、无网络外发） |
| **导入已导出的聊天记录文件**（推荐·跨平台） | 日常生产使用 | ★★★★☆ | 把记录导出成文件或 .zip 压缩包（导出途径由用户自行掌握） |
| **读取已解密的微信数据库** | 已有解密库的场景 | ★★☆☆☆ | 已解密的 `MicroMsg.db` + `MSG*.db`（或直接选整包 .zip 存档） |

### wx4 直读模式（Windows 微信 4.x 加密库）

自动探测本机已安装的微信 4.x、定位账号数据目录、读取加密数据库。**无需手动导出**，一键加载。

**首次使用流程：**
1. 确保微信已登录并处于运行状态
2. 在 GUI 中选择「直接读取本机微信（Windows 4.x）聊天记录」数据源
3. 点「加载聊天对象」→ 自动扫描内存获取密钥（无注入、无重启）并缓存 → 解密库 → 加载联系人
4. 密钥会被加密保存在本地（DPAPI 加密），后续使用无需重复抓取

**命令行模式：**
```bash
# 列出可用数据源（确认 wx4 可用）
python cli.py list-sources

# 抓取数据库密钥（需微信运行中）
python cli.py wx4-capture

# 加载联系人列表
python cli.py list-contacts --source wx4

# 生成总结
python cli.py run --source wx4 --all --preset 30d --out 总结.xlsx
```

**技术原理：**
- 只读扫描微信进程内存（ReadProcessMemory），定位数据库密钥
- 使用 SQLCipher4 参数（PBKDF2-SHA512 × 256000, AES-256-CBC）解密数据库
- 解密后的数据仅暂存于 `~/.wxcsm/wx4_staging/`，使用后自动清理
- 密钥通过 DPAPI 加密存储，仅本机当前用户可解密

### 导入模式的文件约定

选一个文件夹，**一个文件 = 一个聊天对象，文件名即客户名称**：

```
D:\客户沟通记录\
├── 张伟 · 恒瑞医药.csv
├── 分贝通×蔚蓝科技 项目群.txt
└── 赵敏 · 云图科技.json
```

三种格式都自动识别，列名不必完全一致（常见写法都认）：

- **CSV** — 需含时间列与内容列，如 `StrTime,Sender,StrContent,IsSender`
- **TXT** — 形如 `2026-08-20 09:30:00 张伟:` 换行接内容，支持多行消息
- **JSON** — 数组，元素含时间与内容字段

### 合规提醒

提取客户沟通记录前，请确认已获得公司授权，并遵守个人信息保护相关规定与客户约定。
选择「仅用本地生成」时，聊天内容**不会离开本机**。

---

## 四、AI 总结设置（可选）

菜单 **设置 → AI 总结设置**，填任一 OpenAI 兼容接口：

| 项 | 示例 |
|---|---|
| 接口地址 | `https://api.moonshot.cn/v1` |
| 模型名称 | `kimi-k2-turbo-preview` |
| API Key | `sk-...` |

密钥存在 `%USERPROFILE%\.wxcsm\config.json`，不随工具分发。
也可用环境变量注入（优先级更高）：`MOONSHOT_API_KEY` / `OPENAI_API_KEY` / `WXCSM_API_KEY`。

**没有密钥也能用** —— 本地抽取式引擎零依赖零成本，断网可用，只是文字不如 AI 顺滑。

---

## 五、命令行入口（供自动化 / WorkBuddy 调用）

GUI 与 CLI 共用 `core/` 下同一套引擎，行为完全一致。

```bash
# 看有哪些数据源
python cli.py list-sources

# 列出可选聊天对象（支持关键词过滤）
python cli.py list-contacts --source import --path D:/客户沟通记录 --search 恒瑞 --json

# 生成总结并写 Excel
python cli.py run --source import --path D:/客户沟通记录 \
                  --contacts "恒瑞,蔚蓝" --preset 30d \
                  --out D:/客户沟通总结.xlsx --json

# 全部对象 + 自定义时间范围 + 强制本地引擎
python cli.py run --source import --path D:/客户沟通记录 --all \
                  --start 2026-08-01 --end 2026-08-31 --engine offline

# 解密数据库存档（直接喂 .zip）
python cli.py run --source wxdb --path D:/wechat_archive.zip \
                  --contacts "分贝通董事-吃饭群" --preset 30d \
                  --out D:/客户沟通总结.xlsx --json

# 启动图形界面
python cli.py gui
```

`--preset` 可选：`7d` `30d` `this_month` `last_month` `this_quarter` `today`
加 `--json` 输出机器可读结果，便于上游编排。

### WorkBuddy 接入

已注册 Skill **`wechat-csm-summary`**，在 WorkBuddy 里直接用自然语言驱动：

> 「把近 30 天所有客户的微信沟通记录整理成总结，导出到桌面台账」

---

## 六、目录结构

```
wxcsm/
├── 启动工具.exe          ← 业务人员双击这个（真·单文件，无需 Python）
├── 启动工具.bat          ← 备用的 Python 启动器（优先调用上面的 exe）
├── app.py                图形界面
├── cli.py                命令行入口
├── requirements.txt
├── make_installer.py     Windows 安装包构建（产出 .exe Setup）
├── installer_main.py     Windows 安装向导本体
├── make_macos.py         macOS 打包脚本（PyInstaller 产出 .app，可选 .dmg；须在 Mac / CI 运行）
├── verify_macos.py       macOS 产物校验（.app 结构/符号链接/依赖、.dmg 校验、mac_wx4 自检、启动冒烟）
├── .github/workflows/build-macos.yml   GitHub Actions 在 macOS runner 上构建并校验 .app/.dmg
└── core/
    ├── models.py         数据模型
    ├── pipeline.py       业务流水线（GUI 与 CLI 共用）
    ├── summarizer.py     AI + 本地双引擎总结
    ├── exporter.py       Excel 三列导出
    ├── config.py         配置读写
    └── sources/          可插拔数据源
        ├── base.py       适配器接口
        ├── demo.py       内置样例
        ├── importer.py   文件导入（支持 .zip 自动解压）
        ├── sqlite_db.py  已解密数据库（只读，支持 .zip 自动解压）
        └── mac_wx4.py     macOS 微信直读适配器（见 core/mac_wx4/）
    ├── mac_wx4/          macOS 微信直读引擎（Mach VM 扫钥+解密；尽力实现，需真机验证）
```

想接企业微信会话存档、飞书或其他渠道？继承 `core/sources/base.py` 里的 `ChatSource`，
实现 `list_contacts` 与 `fetch` 两个方法，在 `sources/__init__.py` 注册即可。
界面、总结、导出一行都不用改。

---

## 七、常见问题

**Q：点了「加载聊天对象」提示找不到文件？**
检查数据路径是否选对。导入模式要选**文件夹**，不是单个文件。

**Q：导出时报「文件被占用」？**
Excel 正开着这个文件，关掉再导一次。

**Q：总结里说「该时间范围内没有聊天记录」？**
时间范围内确实没有消息。换个范围，或用「近 30 天」先试。

**Q：AI 总结失败了怎么办？**
「智能」模式会自动降级到本地引擎并在结果里标注原因，不会中断整批任务。
结果表格「字数/方式」列会显示这一篇用的是 AI 还是本地。

**Q：总结字数不满意？**
第 ④ 步可以调字数区间，默认 50–200 字。

---

## 八、平台说明

wxcsm 的 GUI 与引擎（`app.py` / `cli.py` / `core/`）基于 tkinter，本身跨平台（Windows / macOS）。

- **Windows**：当前主要交付形态。双击安装向导即可使用，支持 4.x 加密库直读（wx4）、导入文件、已解密库等数据源。
- **macOS**：同样的 GUI 与引擎基于 tkinter 可运行。macOS 端**不支持 wx4「直读本机微信」数据源**（其为 Windows 微信 4.x 专属机制），但内置样例 / 导入文件 / 已解密库三种数据源以及总结、AI、Excel 导出均可正常使用。

macOS 版由 Windows 开发机上完成兼容化与静态/导入自检。**CI 已在真实 macOS runner 上对构建产物做自动化校验**（`.app` 结构/符号链接/动态库依赖、`.dmg` 挂载校验、`mac_wx4` 模块自检、启动冒烟）；但**尚无真实 macOS 微信环境做过端到端取钥解密验证**，需在装好微信的 Mac 上按 `docs/macOS微信直读_真机步骤.md` 实跑一次确认。取钥与解密均由内置纯 Python 实现完成，不依赖任何外部第三方组件。

### macOS 打包与运行

- **直接运行**：`python3 app.py`（需带 tkinter 的 python3，macOS 系统自带）。
- **打包成 .app / .dmg**：`pip3 install --user pyinstaller` 后执行 `python3 make_macos.py`（加 `--dmg` 额外产出 dmg）。
- **校验产物**：`python3 verify_macos.py`（可选 `--require-dmg`、`--no-launch`）。会检查 `.app` 内符号链接与权限位是否完好、非系统动态库依赖能否解析、`.dmg` 能否 `hdiutil verify`/挂载、`mac_wx4` 在真 Darwin 上能否 import 且注册进数据源列表，并尝试拉起 `.app` 观察是否秒退。
- **自动构建 + 自动校验（无需本机 Mac）**：推送到 GitHub 后由 `.github/workflows/build-macos.yml` 在 macOS runner 上构建并校验，从 Actions Artifact `wxcsm-macos` 下载。
- **下载物怎么选（重要）**：选 **`.dmg`** —— 它是自包含磁盘映像，符号链接与权限位都完好。Artifact 里的东西会被打包成 zip，而 **zip 不保留符号链接/权限位**，所以不要用从 zip 里解出的 `.app` 目录（会启动失败）。若需要 `.app` 本体，用同一 artifact 里的 `微信客户沟通总结工具.app.tar.gz`（tar 保留符号链接）。
- **Gatekeeper**：未签名 .app 首次打开会被拦截，右键 →「打开」放行一次，或执行 `xattr -dr com.apple.quarantine 微信客户沟通总结工具.app`。
- **卸载**：把 .app 从 `应用程序` 拖到废纸篓即可。
- 详见 `docs/macOS版本说明.md`。
