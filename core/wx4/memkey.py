"""微信 4.x 只读内存取钥模块 + V4 风格内存扫描。

核心承诺:
  - 不注入代码、不 patch 进程、不调用任何写进程内存的 API;
  - 不强行重启微信;仅在用户明确同意下可帮忙重启后抓取登录窗口;
  - 无任何网络外发;
  - 只读扫描(CryptProtectMemory 保护区读不到是已知边界)。

策略:
  1. V4 内存扫描(首选) — 搜索 GetKeyAddrStub 模式,从指针取密钥候选,
     通过 XOR + PBKDF2 + HMAC 验证。不需要重启微信。
  2. 快速扫描(主) — 在已运行进程内存中搜索 x'<64hex>..' 模式与 32B 随机序列
  3. 邻域精扫 — 找到文件盐后,在 ±4096 范围内用 KDF 验证候选密钥
  4. 重启模式(可选) — 关闭微信→重开→从零开始高频监测
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import hashlib
import hmac as hmac_mod
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

try:  # 优先 PyCryptodome;不可用时回退纯 stdlib(hashlib.pbkdf2_hmac)
    from Crypto.Cipher import AES
    from Crypto.Protocol.KDF import PBKDF2
    from Crypto.Hash import SHA512 as _SHA512
    _HAVE_CRYPTO = True
except Exception:  # pragma: no cover
    AES = None
    PBKDF2 = None
    _SHA512 = None
    _HAVE_CRYPTO = False

    def _pbkdf2_sha512(pw: bytes, salt: bytes, count: int, dklen: int = 32) -> bytes:
        return hashlib.pbkdf2_hmac("sha512", pw, salt, count, dklen=dklen)

from .errors import KeyNotFoundError, WechatNotRunningError
from .locate import WxAccount, detect_wechat_env, running_weixin_pids

# ---- ctypes 常量 ----
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
DATA_PROTECTS = {0x02, 0x04, 0x08}
PAGE = 4096
RESERVE = 80
IV_OFF = PAGE - RESERVE
SQLITE_MAGIC = b"SQLite format 3\x00"
HEX64 = re.compile(rb"x'([0-9a-fA-F]{64})'")
HEX96 = re.compile(rb"x'([0-9a-fA-F]{96})'")
# 64 位连续十六进制(无 x' 前缀,用于某些版本)
RAW_HEX64 = re.compile(rb"\b([0-9a-fA-F]{64})\b")


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wt.DWORD), ("PartitionId", wt.WORD),
                ("RegionSize", ctypes.c_size_t), ("State", wt.DWORD),
                ("Protect", wt.DWORD), ("Type", wt.DWORD)]


# ---- 进程内存读取 ----
def _open_process(pid: int):
    return ctypes.windll.kernel32.OpenProcess(
        PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)


def _data_regions(pid: int) -> Tuple[Optional[int], List[Tuple[int, int]]]:
    """返回 (h, [(base, size), ...]) 只包含可读递交数据区域。"""
    h = _open_process(pid)
    if not h:
        return None, []
    kernel32 = ctypes.windll.kernel32
    out = []
    addr = 0
    while True:
        mbi = MBI()
        if kernel32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(MBI)) == 0:
            break
        if mbi.State == MEM_COMMIT and (mbi.Protect & 0xFF) in DATA_PROTECTS and mbi.RegionSize >= PAGE:
            out.append((mbi.BaseAddress, mbi.RegionSize))
        nxt = (mbi.BaseAddress or 0) + mbi.RegionSize
        if nxt <= addr:
            break
        addr = nxt
    return h, out


def _read_memory(h: int, base: int, size: int, max_chunk: int = 32 * 1048576) -> bytes:
    """读取进程内存,失败位置补零以保持偏移对齐。"""
    data = b""
    for off in range(0, size, max_chunk):
        n = min(max_chunk, size - off)
        buf = ctypes.create_string_buffer(n)
        got = ctypes.c_size_t(0)
        if ctypes.windll.kernel32.ReadProcessMemory(
            h, ctypes.c_void_p(base + off), buf, n, ctypes.byref(got)
        ) and got.value:
            data += bytes(buf.raw[:got.value])
        else:
            data += b"\x00" * n
    return data


# ---- 密钥验证 ----
def _probe_candidates(candidates: List[bytes], salt: bytes, iv: bytes, ct: bytes,
                      workers: int = 8) -> Optional[str]:
    """并行测试候选密钥,返回第一个通过的 hex 密钥或 None。"""
    dk_iv = iv
    dk_ct = ct

    def check(cand: bytes) -> Optional[str]:
        if len(cand) != 32:
            return None
        # 先试 raw mode (AES key == candidate)
        try:
            if AES is not None and AES.new(cand, AES.MODE_CBC, dk_iv).decrypt(dk_ct)[:16] == SQLITE_MAGIC:
                return cand.hex()
        except Exception:
            pass
        # 再试 KDF mode (raw key -> PBKDF2 -> page key)
        try:
            if _HAVE_CRYPTO:
                dk = PBKDF2(cand, salt, dkLen=32, count=256000, hmac_hash_module=_SHA512)
            else:
                dk = _pbkdf2_sha512(cand, salt, 256000, 32)
            if AES is not None and AES.new(dk, AES.MODE_CBC, dk_iv).decrypt(dk_ct)[:16] == SQLITE_MAGIC:
                return cand.hex()
        except Exception:
            pass
        return None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(check, c): c for c in candidates}
        for f in as_completed(futs):
            try:
                r = f.result()
                if r:
                    return r
            except Exception:
                continue
    return None


# ---- 扫描策略 ----

def _scan_hex_strings(data: bytes) -> List[bytes]:
    """提取 x'...' 格式的 64-hex 或 96-hex 字符串(去掉前缀后缀)。"""
    out = []
    for pat in (HEX64, HEX96):
        for m in pat.finditer(data):
            val = m.group(1).lower()
            if len(val) >= 64:
                out.append(bytes.fromhex(val[:64]))
    # 也搜无前缀的 64 位连续 hex
    for m in RAW_HEX64.finditer(data):
        val = m.group(1).lower()
        if isinstance(val, bytes):
            val = val.decode("ascii", errors="ignore")
        if len(val) >= 64:
            out.append(bytes.fromhex(val[:64]))
    return out


def _salt_anchored_hunt(chunks: List[Tuple[int, bytes]], salt: bytes, iv: bytes, ct: bytes,
                        radius: int = 1024, stride: int = 4) -> Optional[str]:
    """在盐命中位置附近搜索密钥(邻域内逐窗口 KDF)。"""
    windows = []
    for base, data in chunks:
        pos = 0
        while True:
            i = data.find(salt, pos)
            if i < 0:
                break
            lo = max(0, i - radius)
            hi = min(len(data), i + radius)
            for off in range(lo, hi - 31, stride):
                windows.append(data[off:off + 32])
            pos = i + 1
    if not windows:
        return None
    return _probe_candidates(windows, salt, iv, ct)


def _blind_aes_scan(chunks: List[Tuple[int, bytes]], iv: bytes, ct: bytes,
                    stride: int = 4, workers: int = 8) -> Optional[str]:
    """盲扫全局:每个 32B 窗口作为 AES 密钥候选。
    NOTE: 仅适用于原始 AES key 直接存在于内存中的版本(如 WCDB 旧版)。
    4.1.13 经验证不适用,保留作为降级策略。
    """
    candidates = []
    for base, data in chunks:
        for i in range(0, len(data) - 31, stride):
            candidates.append(data[i:i + 32])
    return _probe_candidates(candidates, b"", iv, ct, workers=workers)


# ---- 主入口 ----

def capture_key(account: WxAccount,
                progress_cb: Optional[Callable[[str], None]] = None,
                timeout: int = 240,
                restart: bool = False) -> str:
    """从微信进程抓取数据库密钥。

    策略:
      1. V4 内存扫描(首选) — 搜索 GetKeyAddrStub 模式,从指针取密钥候选,
         通过 XOR + PBKDF2 + HMAC 验证。不需要重启微信。
      2. 传统内存扫描(兜底) — 搜索 x'hex 字符串 / 盐锚邻域。

    restart: 保留兼容参数；本实现不依赖 DLL 注入，重启与否不影响主流程。
    timeout: 最大等待秒数。

    返回 64 位十六进制密钥字符串。
    """
    log = progress_cb or (lambda s: None)

    # 准备验证数据
    db = account.message_dbs[0] if account.message_dbs else account.contact_db
    if not db:
        raise KeyNotFoundError("账号下没有可用的加密库文件")
    with open(db, "rb") as f:
        head = f.read(PAGE)
    if head[:16] == SQLITE_MAGIC:
        raise KeyNotFoundError("库文件未加密(已是明文)")
    salt = head[:16]
    iv = head[IV_OFF:IV_OFF + 16]
    ct = head[16:32]

    # ---- 策略 1: V4 内存扫描(不重启) ----
    pids = running_weixin_pids()
    if pids:
        log("微信运行中,尝试 V4 内存扫描(不重启)...")
        try:
            from .v4_key import v4_scan_key
            main_pid = max(pids, key=lambda p: _get_ws(p))
            log(f"使用主进程 PID={main_pid}")
            key = v4_scan_key(
                pid=main_pid,
                db_path=db,
                progress_cb=log,
            )
            if key:
                log("V4 内存扫描成功!")
                return key
            log("V4 扫描未找到密钥,降级到传统扫描...")
        except Exception as e:
            log(f"V4 扫描异常: {e}, 降级...")

    # ---- 策略 2: 传统内存扫描 ----
    if pids:
        log("尝试传统内存扫描...")
        for pid in pids:
            key = _scan_pid(pid, salt, iv, ct, log)
            if key:
                log(f"传统内存扫描成功! PID={pid}")
                return key

    # 仅使用只读内存扫描(纯 Python + ctypes),不注入、不依赖任何第三方库/DLL。
    raise KeyNotFoundError(
        "所有策略均未找到密钥。\n"
        "请打开并登录微信后重试（保持登录态），工具会以只读方式扫描内存取钥。")


def _get_ws(pid: int) -> int:
    """获取进程的 WorkingSet 大小。"""
    try:
        k32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        h = k32.OpenProcess(0x0400 | 0x0010, False, pid)
        if h:
            try:
                # PROCESS_MEMORY_COUNTERS 结构
                class PMC(ctypes.Structure):
                    _fields_ = [
                        ("cb", wt.DWORD),
                        ("PageFaultCount", wt.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t),
                    ]
                pmc = PMC()
                pmc.cb = ctypes.sizeof(PMC)
                if psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), ctypes.sizeof(pmc)):
                    return pmc.WorkingSetSize
            finally:
                k32.CloseHandle(h)
    except Exception:
        pass
    return 0


def _scan_pid(pid: int, salt: bytes, iv: bytes, ct: bytes,
              log: Callable[[str], None]) -> Optional[str]:
    h, regs = _data_regions(pid)
    if not h:
        log(f"  PID {pid} 无法打开进程(可能权限不足)")
        return None
    try:
        total = sum(s for _, s in regs)
        log(f"  读取 {total // 1048576}MB 进程内存...")
        t0 = time.time()
        chunks = []
        for base, size in regs:
            if size > 300 * 1048576:
                continue
            data = _read_memory(h, base, size)
            chunks.append((base, data))
        elapsed = time.time() - t0
        log(f"  读取完成, {len(chunks)} 块, 耗时 {elapsed:.1f}s")

        # 策略 1: x'...' hex 字符串
        log(f"  搜索 x'...' 模式...")
        t0 = time.time()
        hex_cands = []
        for base, data in chunks:
            hex_cands.extend(_scan_hex_strings(data))
        if hex_cands:
            log(f"  找到 {len(hex_cands)} 个 hex 候选, 验证中...")
            key = _probe_candidates(hex_cands, salt, iv, ct)
            if key:
                log(f"  ✓ 通过 x'...' 模式找到密钥!")
                return key
        log(f"  x'...' 模式未命中 ({time.time()-t0:.1f}s)")

        # 策略 2: 盐锚邻域搜索
        log(f"  搜索盐锚邻域...")
        t0 = time.time()
        key = _salt_anchored_hunt(chunks, salt, iv, ct, radius=512, stride=4)
        if key:
            log(f"  ✓ 通过盐锚邻域找到密钥!")
            return key
        log(f"  盐锚邻域未命中 ({time.time()-t0:.1f}s)")

        return None
    finally:
        ctypes.windll.kernel32.CloseHandle(h)