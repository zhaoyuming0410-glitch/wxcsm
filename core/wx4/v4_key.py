"""微信 4.x (V4) 内存扫描取钥 — 干净自包含实现。

本模块**不依赖** pymem / yara-python / pycryptodome,只用:
  - ctypes (Windows API: OpenProcess / VirtualQueryEx / ReadProcessMemory)
  - hashlib / hmac (标准库 PBKDF2-SHA512 + HMAC-SHA512)
  - 纯 Python 字节模式匹配

算法原理(参考微信 WCDB/SQLCipher4 的密钥结构):
  1. 微信在把口令(passphrase)喂给 PBKDF2 之前,先跟 Weixin.dll 里硬编码的
     32 字节 internal_db_key 做 XOR。这个 internal_db_key 藏在代码段的
     mov rdx, imm64 序列里(见 dll_key_v4.py)。
  2. 微信进程内存里有一段 GetKeyAddrStub 特征,记录的指针指向 32 字节的
     "被 XOR 过的原始密钥(raw_key)"。
  3. 扫描到 raw_key 后,与 internal_db_key XOR 得到真实 passphrase,再走
     SQLCipher4 的 PBKDF2+HMAC 校验。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import hashlib
import hmac as hmac_mod
import os
import struct
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, List, Optional, Tuple

try:
    from .dll_key_v4 import extract_internal_db_keys
except ImportError:  # 独立运行/测试脚本直接用模块文件加载
    import importlib.util

    def _load_sibling(name: str):
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(os.path.dirname(__file__), name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    _dl = _load_sibling("dll_key_v4")
    extract_internal_db_keys = _dl.extract_internal_db_keys

# --- 进程/内存常量 ---
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
PAGE_NOACCESS = 0x01
DATA_PROTECTS = {0x02, 0x04, 0x08}  # PAGE_READONLY / READWRITE / WRITECOPY

# --- SQLCipher 4 常量 ---
PAGE_SIZE = 4096
RESERVE = 80          # 每页末尾保留区 (IV 16 + HMAC 64)
IV_SIZE = 16
HMAC_SIZE = 64
KDF_ITER = 256000
MAC_ITER = 2
KEY_SIZE = 32
SALT_SIZE = 16

# --- GetKeyAddrStub 特征 ---
# 固定部分(偏移 6 开始, 27 字节): 10*0x00 + 0x20 + 7*0x00 + 0x2f + 8*0x00
# NOTE(4.1.13+): 实测该"单字节 0x20/0x2f"模式在现代微信堆里不再稳定命中，
#               密钥实际以 [8B 指针→32B raw_key] 00×8 [0x20=32(8字节值)] 形式存放，
#               见 ANCHOR 扫描。此处保留 FIXED_PART 作为历史版本兜底。
FIXED_PART = bytes([
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x20,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x2f,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
])
# 固定部分前有 6 字节通配(特征总长 33 字节)
PREFIX_WILDCARD = 6

# --- 现代微信(4.1.13+)密钥定位锚点 ---
# 实测密钥在内存中以如下结构被引用:
#   [8B 指针 → 32B raw_key 缓冲]  [8B 0x00]  [8B 值=0x20(=32, key 长度)]
# 即一段 8 字节零 + 8 字节值 0x20(=32) 前面紧跟指向 raw_key 的指针。
# 该 16 字节序列用 C 速度 bytes.find 定位快且准。
ANCHOR_LEN32 = b"\x00" * 8 + struct.pack("<Q", KEY_SIZE)


class MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("PartitionId", wt.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


def _k32() -> ctypes.WinDLL:
    return ctypes.windll.kernel32


def _open_process(pid: int) -> int:
    h = _k32().OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    return h or 0


def _close_handle(h: int) -> None:
    if h:
        _k32().CloseHandle(h)


def _data_regions(pid: int) -> List[Tuple[int, int]]:
    """枚举进程可读的已递交内存区域。"""
    h = _open_process(pid)
    if not h:
        return []
    try:
        out: List[Tuple[int, int]] = []
        addr = 0
        while True:
            mbi = MBI()
            if _k32().VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                     ctypes.sizeof(MBI)) == 0:
                break
            if mbi.State == MEM_COMMIT and (mbi.Protect & 0xFF) in DATA_PROTECTS \
                    and mbi.RegionSize >= 4096:
                out.append((int(mbi.BaseAddress or 0), int(mbi.RegionSize)))
            nxt = (mbi.BaseAddress or 0) + mbi.RegionSize
            if nxt <= addr:
                break
            addr = nxt
        return out
    finally:
        _close_handle(h)


def _read_memory(h: int, base: int, size: int, max_chunk: int = 16 * 1048576) -> bytes:
    """读取进程内存,失败的 chunk 补零。"""
    data = b""
    for off in range(0, size, max_chunk):
        n = min(max_chunk, size - off)
        buf = ctypes.create_string_buffer(n)
        got = ctypes.c_size_t(0)
        if _k32().ReadProcessMemory(h, ctypes.c_void_p(base + off), buf, n,
                                    ctypes.byref(got)) and got.value:
            data += bytes(buf.raw[:got.value])
        else:
            data += b"\x00" * n
    return data


def _read_bytes_at(h: int, address: int, size: int) -> Optional[bytes]:
    """从进程内存读取 size 字节。"""
    buf = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t(0)
    if _k32().ReadProcessMemory(h, ctypes.c_void_p(address), buf, size,
                                ctypes.byref(got)) and got.value == size:
        return bytes(buf.raw)
    return None


def _find_pattern(data: bytes) -> List[int]:
    """搜索 GetKeyAddrStub 固定部分,返回"特征起始偏移"(含 6 字节前缀)。"""
    matches = []
    pos = 0
    while True:
        pos = data.find(FIXED_PART, pos)
        if pos == -1:
            break
        match_offset = pos - PREFIX_WILDCARD
        if match_offset >= 0 and match_offset + 33 <= len(data):
            matches.append(match_offset)
        pos += 1
    return matches


def _scan_anchor_offsets(data: bytes) -> List[int]:
    """搜索现代微信密钥锚点,返回"指针所在偏移"列表。

    锚点布局(16 字节): 8 字节零 + 8 字节值 0x20(=32)。
    紧邻其前的 8 字节即为指向 32B raw_key 缓冲的指针。
    用 C 速度 find,可快速定位候选 key 缓冲地址。
    """
    offsets: List[int] = []
    pos = 0
    while True:
        pos = data.find(ANCHOR_LEN32, pos)
        if pos == -1:
            break
        ptr_off = pos - 8
        if ptr_off >= 0 and ptr_off + 8 <= len(data):
            offsets.append(ptr_off)
        pos += 16  # 跳过本锚点,避免重叠误报
    return offsets


def _scan_padded_len_offsets(data: bytes) -> List[int]:
    """宽松锚点: [8B 指针] 0~16 个 0x00 [8B 值 0x20(=32)]。

    不同微信版本/数据结构的指针→长度布局可能有差异(填充字节数不同),
    用该模式扩大命中覆盖。返回"指针所在偏移"。用 C 速度 regex。
    """
    if len(data) < 16:
        return []
    # 8B 指针(任意值) + 0~16 个 0x00 + 8B 值=0x20
    pattern = b"\x00{0,16}\x20\x00\x00\x00\x00\x00\x00\x00"
    offsets: List[int] = []
    # 用 regex 找,再往前定位指针
    import re as _re
    for m in _re.finditer(pattern, data, _re.DOTALL):
        seq_start = m.start()
        # seq_start 之前可能有 0~16 个 0x00(算入 pattern)或直接是 8B 指针尾
        # 指针 = 值0x20 的 8B 序列之前的连续 8 字节
        # seq_start 指向的是 0x00 序列起点或紧跟 0x20 前;找指针:0x20值前推至它前面8B
        # 0x20 u64 起点 = 从 m 中 0x20 位置
        # 简化为:0x20 8字节值起点前 0~16 零之前的 8 字节是指针
        end_zero = m.start()  # 0x20 序列前是填充零(0~16个),它们起点之前是... 
        # 重新定位:0x20 u64 起始 = 找到 0x20 的位置
        # pattern 匹配的是 0x00{0,16} 后接 0x20...
        # m.start() 可能是 0x00 序列起点(如果填充>0)或 0x20 前(填充=0)
        # 0x20 的 u64 从 (m.end()-8) 开始
        lenval_off = m.end() - 8
        # 从 lenval_off 往前找指针:紧邻或隔若干0x00的8B
        # 指针应结束于第一个非0的(或 lenval_off 前 8~24 字节)
        ptr = None
        # 找 lenval_off 之前最近的 8 字节构成合法指针:通常紧跟填充零前
        # 从 lenval_off-8 起,若那 8 字节大多是零则再往前
        cand_off = lenval_off - 8
        # 填充零在 [lenval_off-? , lenval_off) 区间;从这之前取指针
        # 允许 0~16 零:指针起点在 lenval_off-8-16 .. lenval_off-8 之间
        for try_off in range(lenval_off - 8 - 16, lenval_off - 8 + 1):
            if try_off < 0:
                continue
            v = struct.unpack("<Q", data[try_off:try_off+8])[0]
            # 该位置应为非零指针;其后续到 lenval_off 应为零
            if v == 0 or v > 0x7FFFFFFFFFFF:
                continue
            if all(b == 0 for b in data[try_off+8:lenval_off]):
                ptr = try_off
                break
        if ptr is not None and ptr not in offsets:
            offsets.append(ptr)
    return offsets


def _is_potential_key(candidate: bytes) -> bool:
    """熵/字符分布初筛:过滤掉非密码学随机文本。"""
    if len(candidate) != KEY_SIZE:
        return False
    if all(b == 0 for b in candidate) or all(b == 0xFF for b in candidate):
        return False
    # 相异字节 >= 15;可打印字符 <= 24
    if len(set(candidate)) < 15:
        return False
    if sum(32 <= b <= 126 for b in candidate) > 24:
        return False
    # 熵 >= 4.0
    freq = {}
    for b in candidate:
        freq[b] = freq.get(b, 0) + 1
    try:
        import math
        entropy = -sum((c / KEY_SIZE) * math.log2(c / KEY_SIZE) for c in freq.values())
    except Exception:
        entropy = 8.0
    return entropy >= 4.0


# --------------------------------------------------------------------------- 校验
def _derive_page_key(raw_key: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", raw_key, salt, KDF_ITER, dklen=KEY_SIZE)


def _derive_mac_key(page_key: bytes, salt: bytes) -> bytes:
    mac_salt = bytes(b ^ 0x3A for b in salt)
    return hashlib.pbkdf2_hmac("sha512", page_key, mac_salt, MAC_ITER, dklen=KEY_SIZE)


def _verify_candidate(candidate: bytes, db_head: bytes,
                      internal_db_keys: List[bytes]) -> Optional[bytes]:
    """验证候选密钥。返回真实的 32 字节密钥,或 None。

    流程:
      1. 未提供 internal_db_key -> 直接以 candidate 为 raw_key 派生校验。
      2. 提供了 internal_db_key -> 先 XOR,再用派生结果校验;多个候选逐一尝试。

    使用与 cipher.py 一致的 SQLCipher4 布局:
      - 页1: offset=16(跳过 salt);HMAC 存储在页面末尾保留区。
      - HMAC 覆盖 page[offset:cipher_end] + 页码(小端 4 字节)。
    """
    if len(db_head) < PAGE_SIZE:
        return None
    salt = db_head[:SALT_SIZE]
    offset = SALT_SIZE if True else 0  # 第一页跳过 16 字节 salt
    # 页面末尾保留区: [IV(16)][HMAC-SHA512(64)]
    iv = db_head[PAGE_SIZE - RESERVE:PAGE_SIZE - RESERVE + IV_SIZE]
    stored_hmac = db_head[PAGE_SIZE - RESERVE + IV_SIZE:]
    cipher_end = PAGE_SIZE - RESERVE + IV_SIZE

    # 待校验的候选 raw_key 列表
    raw_keys = []
    if not internal_db_keys:
        raw_keys.append(candidate)
    else:
        for k in internal_db_keys:
            raw_keys.append(bytes(a ^ b for a, b in zip(candidate, k)))
        raw_keys.append(candidate)  # 也尝试不 XOR

    for rk in raw_keys:
        try:
            page_key = _derive_page_key(rk, salt)
            mac_key = _derive_mac_key(page_key, salt)
            m = hmac_mod.new(mac_key, digestmod=hashlib.sha512)
            m.update(db_head[offset:cipher_end])
            m.update(struct.pack("<I", 1))  # 页码1, 小端
            computed = m.digest()
            if hmac_mod.compare_digest(computed, stored_hmac):
                return rk
        except Exception:
            continue
    return None


def _to_hex_key(verify_raw: bytes) -> str:
    return verify_raw.hex()


# --------------------------------------------------------------------------- 主扫描
def v4_scan_key(
    pid: int,
    db_path: Optional[str] = None,
    internal_db_keys: Optional[List[bytes]] = None,
    progress_cb: Optional[Callable[[str], None]] = None,
    timeout: float = 120.0,
) -> Optional[str]:
    """V4 内存扫描取钥。

    Returns:
        成功 -> 十六进制密钥字符串(64 字符);失败 -> None。
    """
    log = progress_cb or (lambda s: None)

    if internal_db_keys is None:
        log("从 Weixin.dll 提取 internal_db_key ...")
        internal_db_keys = extract_internal_db_keys()
        log(f"提取到 {len(internal_db_keys)} 个候选 internal_db_key")

    # 读数据库头用于校验
    db_head = b""
    if db_path and os.path.isfile(db_path):
        try:
            with open(db_path, "rb") as f:
                db_head = f.read(PAGE_SIZE)
            log(f"已读数据库头: {db_path} ({len(db_head)}B)")
        except Exception as e:
            log(f"读取数据库失败: {e}")

    if len(db_head) < PAGE_SIZE:
        log("警告: 无法读取数据库头(bytes<4096),校验将失败")

    h = _open_process(pid)
    if not h:
        log("无法打开进程")
        return None

    try:
        regions = _data_regions(pid)
        total_size = sum(s for _, s in regions)
        log(f"发现 {len(regions)} 个区域, 共 {total_size // 1048576}MB")

        t_start = time.time()
        max_cand = 3000  # 安全上限,防极端进程产生海量误候选

        def _collect_and_verify(locators, tag: str) -> Optional[str]:
            """用给定定位器收集候选缓冲并并行 HMAC 校验;命中即返回密钥 hex。"""
            seen_buffers: set = set()
            cand_buffers: List[bytes] = []
            scanned = 0
            for idx, (base, size) in enumerate(regions):
                if time.time() - t_start > timeout:
                    log(f"超时({timeout:.0f}s), 已检查 {idx}/{len(regions)} 区域")
                    break
                if size > 300 * 1048576:
                    continue
                data = _read_memory(h, base, size)
                if len(data) < 33:
                    continue
                ptr_offsets: List[int] = []
                for loc in locators:
                    try:
                        offs = loc(data)
                    except Exception:
                        continue
                    for o in offs:
                        if 0 <= o and o + 8 <= len(data) and o not in ptr_offsets:
                            ptr_offsets.append(o)
                if not ptr_offsets:
                    continue
                scanned += 1
                for off in ptr_offsets:
                    ptr = struct.unpack("<Q", data[off:off + 8])[0]
                    if ptr == 0 or ptr > 0x7FFFFFFFFFFF:
                        continue
                    candidate = _read_bytes_at(h, ptr, KEY_SIZE)
                    if candidate is None or not _is_potential_key(candidate):
                        continue
                    if candidate in seen_buffers:
                        continue
                    seen_buffers.add(candidate)
                    cand_buffers.append(candidate)
                    if len(cand_buffers) >= max_cand:
                        break
                if len(cand_buffers) >= max_cand:
                    break
            if not cand_buffers:
                return None
            log(f"[{tag}] 收集到 {len(cand_buffers)} 个候选, 耗时 {time.time()-t_start:.0f}s")
            n_workers = max(2, min(4, os.cpu_count() or 2))
            tested = 0
            with ThreadPoolExecutor(max_workers=n_workers) as ex:
                futs = {ex.submit(_verify_candidate, c, db_head, internal_db_keys): c
                        for c in cand_buffers}
                for f in as_completed(futs):
                    verify_raw = f.result()
                    tested += 1
                    if verify_raw:
                        key_hex = verify_raw.hex()
                        log(f"[{tag}] 找到有效密钥 (#{tested}/{len(cand_buffers)}, "
                            f"用时 {time.time()-t_start:.0f}s)")
                        return key_hex
            return None

        # 第 1 轮: 快速严格锚点(实测命中, ~百毫秒级定位), 正常即在此命中。
        fast = _collect_and_verify(
            (_scan_anchor_offsets, _find_pattern), "严格锚点")
        if fast:
            return fast
        # 第 2 轮(仅第 1 轮失败时): 宽松锚点覆盖不同版本/布局, 提高命中率。
        broad = _collect_and_verify(
            (_scan_padded_len_offsets,), "宽松锚点")
        if broad:
            return broad
        log(f"扫描完成: 未找到密钥, 耗时 {time.time()-t_start:.0f}s")
        return None
    finally:
        _close_handle(h)


def find_wechat_pid() -> Optional[int]:
    """找微信主进程 PID(WS 最大者)。"""
    try:
        from .locate import detect_wechat_env
        env = detect_wechat_env()
        if env and env.pid:
            return env.pid
    except Exception:
        pass
    return None


if __name__ == "__main__":
    import sys
    pid = find_wechat_pid()
    print(f"微信 PID: {pid}")
    if not pid:
        sys.exit(1)
    db = sys.argv[1] if len(sys.argv) > 1 else None
    key = v4_scan_key(pid, db_path=db, progress_cb=print)
    if key:
        print(f"\n[KEY] {key}")
    else:
        print("\n未找到密钥")
