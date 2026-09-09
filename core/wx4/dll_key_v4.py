"""从 Weixin.dll 的代码段中提取 32 字节 internal_db_key 候选。

无第三方依赖(不依赖 pefile): 自己解析 PE 头定位代码段,再用机器码模式匹配。

原理(参考微信 SQLCipher 初始化的机器码特征):
    一个连续的指令序列:
        mov rdx, imm64    ; 48 BA <8字节>    (第1段)
      [3-8 字节任意指令]
        mov rdx, imm64    ; 48 BA <8字节>
      [3-8 字节任意指令]
        mov rdx, imm64    ; 48 BA <8字节>
      [3-8 字节任意指令]
        mov rdx, imm64    ; 48 BA <8字节>
      [3-8 字节任意指令]
        test rax, rax     ; 48 85 C0
    把这 4 个 imm64 依次拼接成 32 字节,即为候选 internal_db_key。

这是微信把 Codec key 常量以 imm64 形式写入 sqlite3_key_v2 初始化逻辑的典型产物。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Optional

# 机器码模式: 4 段 mov rdx, imm64 + 中间填充 + test rax, rax
# 每段: 48 BA = mov rdx, imm64 (10 字节: 48 BA + 8 字节立即数)
PATTERN = re.compile(
    b"^\x48\xBA(.{8})"      # mov rdx, <8 bytes>  -> group 1
    b".{3,8}?"              # 中间任意 3-8 字节
    b"\x48\xBA(.{8})"       # mov rdx, ...        -> group 2
    b".{3,8}?"
    b"\x48\xBA(.{8})"       # mov rdx, ...        -> group 3
    b".{3,8}?"
    b"\x48\xBA(.{8})"       # mov rdx, ...        -> group 4
    b".{3,8}?"
    b"\x48\x85\xC0",        # test rax, rax
    re.DOTALL,
)

# 代码段特征(IMAGE_SCN_CNT_CODE = 0x00000020 | IMAGE_SCN_MEM_EXECUTE = 0x20000000 | IMAGE_SCN_MEM_READ = 0x40000000)
# 用 0x20000000 (MEM_EXECUTE) 作为主要判据
CODE_SECTION_SCN = 0x60000020  # EXECUTE|READ|CNT_CODE
SCN_MEM_EXECUTE = 0x20000000

# 解析用的 PE 常量
IMAGE_FILE_MACHINE_AMD64 = 0x8664


def extract_internal_db_keys(dll_path: Optional[str] = None) -> List[bytes]:
    """从 Weixin.dll 提取所有候选 internal_db_key(32 字节)。

    Returns:
        去重后的候选密钥字节列表。
    """
    if dll_path is None:
        dll_path = _find_weixin_dll()
    if not dll_path:
        return []
    if not os.path.isfile(dll_path):
        return []

    try:
        with open(dll_path, "rb") as f:
            data = f.read()
    except Exception:
        return []

    # --- 解析 PE 头, 定位代码段 ---
    try:
        image_base, code_ranges = _locate_code_sections(data)
    except Exception:
        return []

    if not code_ranges:
        return []

    # --- 在每个代码段内扫描机器码模式 ---
    found = []
    seen = set()
    for (file_offset, size) in code_ranges:
        chunk = data[file_offset:file_offset + size]
        keys = _scan_chunk(chunk)
        for k in keys:
            if k not in seen and len(k) == 32:
                seen.add(k)
                found.append(k)

    return found


def _find_weixin_dll() -> Optional[str]:
    """定位 Weixin.dll。优先复用 locate.detect_wechat_env,再兜底全局搜索。"""
    # 1. 从微信安装目录
    try:
        from .locate import detect_wechat_env
        env = detect_wechat_env()
        if env and env.exe_path:
            base = os.path.dirname(env.exe_path)
            # 版本子目录 (如 4.1.13.12\Weixin.dll)
            for cand in [os.path.join(base, "Weixin.dll")]:
                if os.path.isfile(cand):
                    return cand
            # 遍历 base 下的版本子目录
            for d in sorted(os.listdir(base), reverse=True):
                cand = os.path.join(base, d, "Weixin.dll")
                if os.path.isfile(cand):
                    return cand
    except Exception:
        pass

    # 2. 兜底: 常见安装根 + 版本子目录
    for root in [r"D:\Program Files\Tencent\Weixin", r"C:\Program Files\Tencent\Weixin"]:
        if os.path.isdir(root):
            if os.path.isfile(os.path.join(root, "Weixin.dll")):
                return os.path.join(root, "Weixin.dll")
            for d in sorted(os.listdir(root), reverse=True):
                cand = os.path.join(root, d, "Weixin.dll")
                if os.path.isfile(cand):
                    return cand
    return None


def _locate_code_sections(data: bytes):
    """解析 PE 头,返回 (image_base, [(代码段文件偏移, 大小), ...])。

    仅支持 32/64 位 PE,不依赖 pefile。
    """
    if len(data) < 0x40:
        raise ValueError("文件太小,不是有效 PE")

    # DOS 头 e_lfanew @ 0x3C (4 字节)
    e_lfanew = int.from_bytes(data[0x3C:0x40], "little")
    if e_lfanew + 0x18 > len(data):
        raise ValueError("e_lfanew 越界")

    # PE 签名
    magic = data[e_lfanew:e_lfanew + 4]
    if magic != b"PE\x00\x00":
        raise ValueError("不是 PE 文件")

    # COFF 头 @ e_lfanew+4
    coff = e_lfanew + 4
    machine = int.from_bytes(data[coff:coff + 2], "little")
    number_of_sections = int.from_bytes(data[coff + 2:coff + 4], "little")
    size_of_optional_header = int.from_bytes(data[coff + 16:coff + 18], "little")

    # 可选头 @ coff+20
    optional = coff + 20
    opt_pe = data[optional:optional + 2]
    if opt_pe == b"\x0b\x02":          # PE32+
        image_base = int.from_bytes(data[optional + 24:optional + 32], "little")
        section_alignment = int.from_bytes(data[optional + 32:optional + 36], "little")
        file_alignment = int.from_bytes(data[optional + 36:optional + 40], "little")
    elif opt_pe == b"\x0b\x01":        # PE32
        image_base = int.from_bytes(data[optional + 28:optional + 32], "little")
        section_alignment = int.from_bytes(data[optional + 32:optional + 36], "little")
        file_alignment = int.from_bytes(data[optional + 36:optional + 40], "little")
    else:
        raise ValueError("未知的可选头格式")

    # 节表 @ optional+size_of_optional_header
    section_table = optional + size_of_optional_header
    code_ranges = []
    for i in range(number_of_sections):
        off = section_table + i * 40
        if off + 40 > len(data):
            break
        name = data[off:off + 8].rstrip(b"\x00").decode("utf-8", "ignore")
        virtual_size = int.from_bytes(data[off + 8:off + 12], "little")
        virtual_addr = int.from_bytes(data[off + 12:off + 16], "little")
        size_of_raw_data = int.from_bytes(data[off + 16:off + 20], "little")
        ptr_raw_data = int.from_bytes(data[off + 20:off + 24], "little")
        characteristics = int.from_bytes(data[off + 36:off + 40], "little")

        # 代码段: 有 MEM_EXECUTE 特性
        if (characteristics & SCN_MEM_EXECUTE) and size_of_raw_data > 0:
            code_ranges.append((ptr_raw_data, size_of_raw_data))

    return image_base, code_ranges


def _scan_chunk(chunk: bytes) -> List[bytes]:
    """在单个代码段字节中搜索机器码模式,返回 32 字节密钥候选。"""
    results = []
    offset = 0
    while True:
        idx = chunk.find(b"\x48\xBA", offset)
        if idx == -1:
            break
        match = PATTERN.match(chunk[idx:idx + 85])
        if match:
            key_bytes = match.group(1) + match.group(2) + match.group(3) + match.group(4)
            results.append(key_bytes)
            offset = idx + len(match.group(0))
        else:
            offset = idx + 1
    return results


if __name__ == "__main__":
    keys = extract_internal_db_keys()
    print(f"提取到 {len(keys)} 个候选 internal_db_key")
    for k in keys[:10]:
        print(" ", k.hex())
