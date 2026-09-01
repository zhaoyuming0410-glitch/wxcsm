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
本工具**刻意不去读微信进程内存**，原因有三：

1. 这类手段每逢微信大版本更新就失效，工具会随时变砖；
2. 可能触发账号风控；
3. 在企业合规审查里难以说清。

因此提供三条取数路径，**上层功能完全一致，随时可切换**：

| 数据源 | 适用场景 | 稳定性 | 需要准备什么 |
|---|---|---|---|
| **内置样例数据** | 试用、培训、验收产出格式 | ★★★★★ | 无 |
| **导入已导出的聊天记录文件**（推荐） | 日常生产使用 | ★★★★☆ | 先用留痕 / WeChatMsg / WeChatDataAnalysis 等工具把记录导出成文件或 .zip 压缩包 |
| **读取已解密的微信数据库** | 已有解密库的场景 | ★★☆☆☆ | 外部工具解密出的 `MicroMsg.db` + `MSG*.db`（或直接选整包 .zip 存档） |

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
├── make_macos.py         macOS 安装包构建（产出 .app / .dmg，需在 Mac 运行）
├── installer_macos.py    macOS 安装向导本体
├── .github/workflows/build-macos.yml   GitHub Actions 自动构建 macOS 包
├── push_via_api.py       受限网络下用 API 上传仓库（替代 git push）
├── wait_download.py      监控构建并下载/校验 artifact（开发辅助）
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
        └── sqlite_db.py  已解密数据库（只读，支持 .zip 自动解压）
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

## 八、macOS 版本

wxcsm 的 GUI 与引擎（`app.py` / `cli.py` / `core/`）本身是跨平台的（tkinter）。除 Windows 外，也已适配 macOS：

- **WDA 启动器**（`core/wda_launcher.py`）在 macOS 上会到 `/Applications`、`~/Applications` 探测 `WeChatDataAnalysis.app`，识别 `.dmg` / `.pkg` / `.zip` 安装包；启动与打开安装统一用 `open` 命令。
- 字体、主题、缺安装包提示均按平台自适应（macOS 用苹方 PingFang SC）。
- 分发形态与 Windows 一致：**带安装向导**，可选安装目录 + 桌面替身。

### 在 Mac 上构建（无法在 Windows 交叉编译）

> Windows 端只能产出 Windows 的 `.exe` / `.app` 安装包；macOS 的 `.app` 必须在 macOS 上用 PyInstaller 生成，本机（Windows）无法直接编译。

前置：macOS 11+，本机 Python 3.11+（系统自带 tkinter；或 `brew install python-tk`），并 `pip install pyinstaller`。

1. 把 macOS 版 WeChatDataAnalysis 安装包（`.dmg` / `.pkg` / `.zip`）放到：
   ```
   wxcsm/tools/wechatdataanalysis/
   ```
   （不放也能构建，但安装后的工具点「启动 WeChatDataAnalysis」会提示缺安装包，需你另行下载再放此目录重构建。）
2. 在 wxcsm 目录、已激活的 venv 下运行：
   ```bash
   python make_macos.py            # 全量：内嵌 WDA 安装包
   python make_macos.py --no-wda  # 精简：不含 WDA 安装包
   python make_macos.py --dmg     # 额外打成 .dmg 便于分发
   ```
3. 产物在 `dist_installer/`：
   - `微信客户沟通总结工具安装向导.app`（双击即安装向导：选目录 + 桌面替身）
   - 可选 `微信客户沟通总结工具安装向导.dmg`
4. 收件人双击「安装向导.app」→ 选安装目录（默认 `~/Applications`，免管理员权限）→ 安装 → 双击生成的「微信客户沟通总结工具.app」即可使用；WeChatDataAnalysis 在首次使用时由按钮打开内嵌安装包，按提示装到 `/Applications`。

### 自动构建（GitHub Actions，无需本地 Mac）

仓库已配置 `.github/workflows/build-macos.yml`：把代码 push 到 `main` 分支，或在 Actions 页手动 **Run workflow**，GitHub 托管的 macOS runner 会自动跑 `make_macos.py --dmg`，把产物（`.app` + `.dmg`）作为 Actions Artifact（名称 `wxcsm-macos`）上传。

- **CI 产出的是「不含 WDA」的精简版**：`tools/` 被 `.gitignore` 忽略，WeChatDataAnalysis 安装包体积大、不适合进 git。需要内嵌 WDA 的全量版，请在自己 Mac 上放好包后本地 `python make_macos.py`（见上）。
- **下载**：Actions 页 → 对应 run → Artifacts → 下载 `wxcsm-macos.zip`（内含 `微信客户沟通总结工具安装向导.app` 与 `.dmg`）。在 Mac 上双击 `.app` 即安装向导；或挂`载`.dmg 拖拽到 `Applications`。
- 工作流需要能写 `.github/workflows/` 的 token 权限（classic PAT 需勾选 `workflow` 作用域）。

### 备注

- **未签名 .app** 在 Mac 上首次打开可能被 Gatekeeper 拦截：右键「打开」一次放行，或在终端执行
  `xattr -dr com.apple.quarantine 微信客户沟通总结工具.app` 解除隔离。
- **卸载**：运行「微信客户沟通总结工具.app」内的「卸载.command」。
- 构建相关脚本：`make_macos.py`（构建）、`installer_macos.py`（安装向导本体）。
- 安装包内部实现：向导 `.app` 把业务程序「启动工具.app」以 `Contents/Resources/wxcsm_payload.bin`（XOR 混淆的 zip）形式内嵌，安装时解压到目标 `.app`；改动 Mach-O 会破坏 ad-hoc 签名，故 payload 不放可执行尾部。
- 仓库与 CI 由一次性 Personal Access Token 配置完成，**建议用完后到 GitHub → Settings → Developer settings → Personal access tokens 撤销该 token**，避免长期泄露。
