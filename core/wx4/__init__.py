"""微信 4.x (Windows) 原生只读取数引擎。

边界与承诺:
  - 不注入代码、不 patch 进程、不改写微信任何文件;
  - 不重启/不要求重新登录(仅在用户主动配合的「重启抓钥」模式下被动等待);
  - 无任何网络外发;
  - 只读进程内存(ReadProcessMemory)+ 只读复制加密库后本机解密。

模块:
  locate   — 探测微信安装、数据目录、账号与运行状态(纯 stdlib + ctypes)
  cipher   — SQLCipher4(微信 4.x 参数)逐页解密,输出可被 sqlite3 直读的明文库
  memkey   — 只读内存取钥:V4 内存扫描(主) + 传统扫描 + 重启窗口被动抓取
  keyring  — 抓到的密钥在本机加密缓存(DPAPI),避免每次重复抓取
  v4_key   — V4 内存扫描取钥(自包含:ctypes + stdlib,无第三方依赖)
  dll_key_v4 — 从 Weixin.dll 提取 internal_db_key(自写 PE 解析器,无 pefile)
"""

from .locate import (
    WxAccount,
    WxEnv,
    detect_wechat_env,
    pick_message_db,
)
from . import cipher, keyring, memkey, v4_key
from .errors import Wx4Error

__all__ = [
    "WxAccount",
    "WxEnv",
    "detect_wechat_env",
    "pick_message_db",
    "Wx4Error",
    "v4_key",
    "cipher",
    "memkey",
]
