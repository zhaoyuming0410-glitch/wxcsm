# -*- coding: utf-8 -*-
"""命令行诊断入口(供在真机 Mac 上运行, 验证 mac wx4 各环节)。

用法:
  python3 -m core.mac_wx4 detect        # 只探测数据目录/账号/库
  python3 -m core.mac_wx4 keys          # 扫描运行中微信内存里的库密钥(salt 数)
  python3 -m core.mac_wx4 decrypt       # detect + keys + 解密到 ~/.wxcsm/mac_wx4_staging

⚠️ 仅 macOS; 需已 sudo 重签微信去掉 Hardened Runtime, 且微信运行并登录。
"""
from __future__ import annotations

import sys


def _detect():
    from . import locate
    accts = locate.detect_mac_accounts()
    if not accts:
        print("未找到 macOS 微信数据目录(db_storage with message)。")
        return
    for a in accts:
        print(f"\n账号 {a.account or '(?)'}:")
        print(f"  db_storage = {a.db_storage}")
        print(f"  message×{len(a.message_dbs)}")
        for m in a.message_dbs[:8]:
            print(f"    - {m}")
        print(f"  contact = {a.contact_db}")
        print(f"  session = {a.session_db}")


def _keys():
    from . import memkey
    print("扫描运行中的微信进程内存…")
    try:
        keys = memkey.scan_wechat_keys(progress=print)
    except Exception as e:
        print(f"取钥失败: {e}")
        return
    print(f"共扫到 {len(keys)} 个 salt→key 条目。")


def _decrypt():
    from . import decrypt, locate, memkey
    accts = locate.detect_mac_accounts()
    if not accts:
        print("未找到账号库, 先跑 detect。")
        return
    keys = memkey.scan_wechat_keys(progress=print)
    if not keys:
        print("内存未取到密钥(微信未运行/未登录/未重签)。")
        return
    a = accts[0]
    src = [*a.message_dbs]
    if a.contact_db:
        src.append(a.contact_db)
    if a.session_db:
        src.append(a.session_db)
    from pathlib import Path
    out_root = Path.home() / ".wxcsm" / "mac_wx4_staging" / (a.account or "acc")
    out_root.mkdir(parents=True, exist_ok=True)
    ok = 0
    for s in src:
        salt = decrypt.probe_salt(str(s))
        if salt is None:
            if s.read_bytes()[:16] == b"SQLite format 3\x00":
                print(f"[明文] {s.name}")
                ok += 1
            continue
        key = keys.get(salt.hex().lower())
        if not key:
            print(f"[缺钥] {s.name} (salt {salt.hex()})")
            continue
        dst = out_root / (s.parent.name + "__" + s.name)
        try:
            decrypt.decrypt_db_file(str(s), key, str(dst), check_hmac=True)
            from . import reader
            good = reader._is_decrypted(dst)
            print(f"[解密] {s.name} -> {'OK' if good else '输出异常'}")
            ok += 1
        except Exception as e:
            print(f"[失败] {s.name}: {e}")
    print(f"\n完成: {ok}/{len(src)} 库处理。解密输出在 {out_root}")


def main() -> int:
    if sys.platform != "darwin":
        print("仅 macOS 可用。当前系统:", sys.platform)
        return 1
    cmd = sys.argv[1] if len(sys.argv) > 1 else "detect"
    fn = {"detect": _detect, "keys": _keys, "decrypt": _decrypt}.get(cmd)
    if fn is None:
        print(__doc__)
        return 2
    fn()
    return 0


if __name__ == "__main__":
    sys.exit(main())
