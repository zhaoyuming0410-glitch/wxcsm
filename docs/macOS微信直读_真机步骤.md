# macOS 微信直读(wx4)真机验证与使用步骤

> 本页描述如何在一台真机 Mac 上，让 wxcsm 的 **「直接读取本机微信（macOS 4.x）」** 数据源工作。
> ⚠️ 该功能在 Windows 开发机上完成，**尚未在真实 macOS 微信上端到端验证**——本页步骤需要在装有微信 4.x 的 Mac 上实际跑通并修正。

## 一、可行性依据

macOS 微信 4.x 与 Windows 4.x 一样使用 **WCDB / SQLCipher 4**（AES-256-CBC 页级加密），
但取钥机制不同：

| 项 | Windows 4.x | macOS 4.x |
|---|---|---|
| 加密 | SQLCipher4，raw_key 需 256000 次 PBKDF2 派生页密钥 | SQLCipher4，但内存里的 enc_key **直接**当 AES-256 密钥用 |
| 每库密钥 | 同账号共一套 | 每 `.db` **独立** enc_key + salt |
| 取钥 | ReadProcessMemory 扫 raw_key | **Mach VM**(task_for_pid / vm_read) 扫 `x'<64hex key><32hex salt>'` |
| 跨进程读内存 | 一般可行 | 微信默认带 **Hardened Runtime** 会阻止，需先重签去掉 |

## 二、真机一次性准备（需要 sudo，系统级操作）

> 因为 macOS 微信默认带 Hardened Runtime，阻止其他进程 `task_for_pid` 读其内存，
> 需把微信改为 ad-hoc 签名去掉该限制。**微信每次更新后都要重做。**

```bash
# 先退出微信
osascript -e 'quit app "WeChat"'
# 用 ad-hoc 签名去掉 Hardened Runtime(会改变签名, 属系统级改动)
sudo codesign --force --deep --sign - /Applications/WeChat.app
# 重新打开微信并登录
open /Applications/WeChat.app
```

> 替代方案：关闭 SIP（`csrutil disable` 后重启）则无需重签，但影响范围更大，不推荐。
> 注意：`task_for_pid` 还要求运行进程具备相应权限；若仍返回错误，需在装有微信的
> Mac 上核对 `core/mac_wx4/memkey.py` 里 `mach_vm_region` 的 ABI/常量（见下方“待真机核对”）。

## 三、在 Mac 上自检

在项目根目录执行（需微信运行中且已登录）：

```bash
# 1) 探测数据目录/账号/库
python3 -m core.mac_wx4 detect

# 2) 扫描微信进程内存里的库密钥(salt 数应为非 0)
python3 -m core.mac_wx4 keys

# 3) 端到端: 定位账号 + 取钥 + 解密到 ~/.wxcsm/mac_wx4_staging
python3 -m core.mac_wx4 decrypt
```

若 1 探测不到账号 → 微信数据不在默认容器路径或版本目录形态不同，可改用
「导入聊天记录文件 / 读取已解密数据库」数据源，或手动把 `db_storage` 路径接入。

## 四、在 GUI / CLI 使用

- **GUI**：数据源下拉框在 macOS 上会多出「直接读取本机微信（macOS 4.x）」。选它 → 加载聊天对象 → 总结/导出。
- **CLI**：`python3 cli.py run --source wx4mac --all --preset 30d --out ~/Desktop/mac_wx4.xlsx`
- Windows 上该数据源不会出现在下拉框，选了也会给出“仅 macOS 可用”的明确提示。

## 五、实现文件

```
core/mac_wx4/
  locate.py      定位 macOS 微信数据目录/账号 db_storage
  memkey.py      Mach VM 扫微信进程内存, 提取 {salt: enc_key}
  decrypt.py     macOS 每库 AES-256-CBC 页级解密(enc_key 直接作 AES 密钥)
  reader.py      扫解密后明文库得到联系人/消息
  data_source.py MacWxLiveSource(ChatSource 适配器, 仅 mac 注册)
  __main__.py    命令行自检入口(detect/keys/decrypt)
```

## 六、待真机核对（如实）

- `memkey.py` 中 `mach_vm_region` 的 `VM_REGION_BASIC_INFO_64` flavor 常量与 `vm_region_basic_info` struct 的 ABI 需在真机核对；若 task_for_pid 后遍历 region 报错，按实际 Mach 头文件调整。
- macOS 微信 4.x 数据目录的账号层（`<account-hex>/xwechat_files/<wxid>/db_storage`）形态随版本变化，`locate.py` 按“db_storage 内含 message/”粗匹配；若命中不到请补目录样本。
- 解密参数（enc_key 直接作 AES 密钥、mac_key = PBKDF2(enc_key, salt^0x3a, 2)、reserve=80、page=4096）按社区公开实现，需用真实库页 1 HMAC 校验确认。
- 消息表/联系人表结构（`Msg_<md5>`、`Name2Id`、`contact` 表）按 4.x 常见结构，若有出入按真机 schema 微调 `reader.py`。

**结论：本功能已按公开资料完成可用架构与诚实门槛，但必须在一台装了微信 4.x 的 Mac 上按上述步骤跑通、按需修正后，才能视为“支持直读”。**
