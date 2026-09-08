# -*- coding: utf-8 -*-
"""macOS 微信 4.x 直读(wx4)数据源 —— 自研、仅 macOS 可用。

⚠️ 状态声明(如实):
  - 本包按公开资料(macOS 微信 4.x / WCDB / SQLCipher4 / Mach VM)独立实现,
    **未在真实 macOS 微信上端到端验证**, 需在装有微信 4.x 的 Mac 上真机确认。
  - 它只读进程内存(Mach task_for_pid / vm_read)+ 只读复制本地库后本机解密,
    不注入代码、不改写微信文件、无网络外发。
  - macOS 微信默认带 Hardened Runtime, 会阻止跨进程读取内存; 需先一次性用
    sudo 把微信 ad-hoc 重签以去掉该限制(见 docs/macOS微信直读_真机步骤.md)。

模块:
  locate  — 定位 macOS 微信数据目录与账号的 db_storage(仅 stdlib)
  memkey  — Mach VM 扫描运行中微信进程内存, 提取 x'<key><salt>' 密钥
  decrypt — macOS 每库 AES-256-CBC 页级解密(内存 enc_key 直接作 AES 密钥)
  reader  — 扫描解密后的明文库得到联系人/消息(尽力而为)
"""
from __future__ import annotations

import sys

IS_MAC = sys.platform == "darwin"
