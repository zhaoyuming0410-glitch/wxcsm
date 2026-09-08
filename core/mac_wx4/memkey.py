# -*- coding: utf-8 -*-
"""Mach VM 内存扫描: 从运行中的 macOS 微信进程提取 SQLCipher 4 数据库密钥。

原理(macOS 微信 4.x):
  WCDB(SQLCipher 4) 加密本地库, 每个 .db 有独立 AES-256 密钥+盐, 进程内存中常以
  x'<64hex_key><32hex_salt>' 或连续 96 hex 字符缓存。遍历微信进程内存, 提取 (key,salt),
  再与各 .db 文件第 1 页的盐匹配。

⚠️ 前提(如实):
  - macOS 微信默认带 Hardened Runtime 阻止跨进程读内存, 须先一次性 ad-hoc 重签微信
    去掉它(或满足系统安全策略)后, 本扫描才可能成功 —— 见 docs 真机步骤。
  - Mach 遍历(mach_vm_region / vm_read)的 struct ABI 依赖真机校准; 本实现按
    vm_region_basic_info_64 稳定结构编写, 若有出入需在装有微信的 Mac 上核对修正。
  - 未在真实 macOS 微信上验证。
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import re
from typing import Dict, Iterator, List, Optional, Tuple

try:
    import psutil as _psutil
    _HAVE_PSUTIL = True
except Exception:
    _HAVE_PSUTIL = False

HEX_RE = re.compile(rb"[0-9a-fA-F]{96,}")

# ------------------------------------------------------------------ 进程枚举
def find_wechat_pids(name: Optional[str] = None) -> List[int]:
    """返回微信进程 pid 列表。优先 psutil; 无 psutil 时用 pgrep。"""
    name = name or "WeChat"
    pids: List[int] = []
    if _HAVE_PSUTIL:
        for p in _psutil.process_iter(["pid", "name"]):
            try:
                if p.info["name"] and name.lower() in p.info["name"].lower():
                    pids.append(p.info["pid"])
            except Exception:
                continue
        return pids
    # 回退: pgrep -x
    try:
        import subprocess
        out = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True)
        return [int(x) for x in out.stdout.split() if x.strip().isdigit()]
    except Exception:
        return []


# ------------------------------------------------------------------ Mach API
# libSystem 里这些符号多数在 libSystem / libproc。task_for_pid + mach_vm_region
# 是 mach 接口, 通常在 libSystem。用 ctypes 延迟加载, 非 mac 上 import 不崩。
def _libsystem() -> ctypes.CDLL:
    name = ctypes.util.find_library("System")
    lib = ctypes.CDLL(name or "libSystem.B.dylib", use_errno=True)
    # --- task_for_pid(mach_port_t target, int pid, mach_port_t* task) ---
    lib.task_for_pid.restype = ctypes.c_int
    lib.task_for_pid.argtypes = [ctypes.c_uint, ctypes.c_int,
                                 ctypes.POINTER(ctypes.c_uint)]
    # --- mach_task_self() ---
    lib.mach_task_self.restype = ctypes.c_uint
    lib.mach_task_self.argtypes = []
    # --- mach_vm_region(mach_port_t task, mach_vm_address_t* addr,
    #                    mach_vm_size_t* size, vm_region_flavor_t flavor,
    #                    vm_region_info_t info, mach_msg_type_number_t* count,
    #                    mach_port_t* obj) ---
    lib.mach_vm_region.restype = ctypes.c_int
    lib.mach_vm_region.argtypes = [
        ctypes.c_uint,
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint),
    ]
    # --- vm_read(mach_port_t task, mach_vm_address_t addr, mach_vm_size_t size,
    #             vm_offset_t* data, mach_msg_type_number_t* dataCnt) ---
    lib.vm_read.restype = ctypes.c_int
    lib.vm_read.argtypes = [
        ctypes.c_uint,
        ctypes.c_ulonglong,
        ctypes.c_ulonglong,
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_uint),
    ]
    # --- vm_deallocate(mach_port_t task, vm_offset_t addr, mach_vm_size_t size) ---
    lib.vm_deallocate.restype = ctypes.c_int
    lib.vm_deallocate.argtypes = [ctypes.c_uint, ctypes.c_ulonglong, ctypes.c_ulonglong]
    # --- mach_vm_deallocate 也可; 上面已够用 ---
    return lib


# vm_region_basic_info_64 稳定结构(按公开 ABI), 仅用于计算 flavor 与 region 大小遍历。
# 注: 真正可靠的 region 遍历依赖 mach_vm_region 的 info flavor 与 count, 真机校准时调整。
VM_REGION_BASIC_INFO_64 = 9          # flavor 常量(Apple 公开头文件)
MAX_INFO_COUNT = 16                  # vm_region_basic_info_data_64_t 的个数上限(足够)


class MachReader:
    """只读遍历某 pid 进程内存并返回大块可读数据(按 region 切, 避免一次性全读)。"""

    def __init__(self, pid: int):
        if os.sys.platform != "darwin":
            raise OSError("Mach VM 扫描仅支持 macOS")
        self.pid = pid
        self._lib = _libsystem()
        self._task = self._open_task()

    def _open_task(self) -> int:
        lib = self._lib
        self_task = lib.mach_task_self()
        task = ctypes.c_uint(0)
        kr = lib.task_for_pid(self_task, self.pid, ctypes.byref(task))
        if kr != 0:
            raise PermissionError(
                f"task_for_pid 失败(kr={kr})。macOS 微信默认带 Hardened Runtime, "
                "需先 sudo 重签微信去掉该限制(见 docs), 或提升权限后再试。")
        return task.value

    def iter_regions(self) -> Iterator[Tuple[int, int]]:
        """Yield (addr, size) 可读 region。用 mach_vm_region 循环。
        flavor 用 VM_REGION_BASIC_INFO_64; 若真机返回不支持则抛清晰错误。"""
        lib = self._lib
        task = self._task
        addr = ctypes.c_ulonglong(0)
        size = ctypes.c_ulonglong(0)
        while True:
            info = (ctypes.c_uint32 * MAX_INFO_COUNT)()
            count = ctypes.c_uint(MAX_INFO_COUNT)
            obj = ctypes.c_uint(0)
            kr = lib.mach_vm_region(
                task, ctypes.byref(addr), ctypes.byref(size),
                VM_REGION_BASIC_INFO_64,
                ctypes.cast(info, ctypes.c_void_p),
                ctypes.byref(count), ctypes.byref(obj))
            if kr != 0:
                break  # 遍历结束或出错
            yield int(addr.value), int(size.value)
            nxt = addr.value + size.value
            if nxt <= addr.value:
                break
            addr = ctypes.c_ulonglong(nxt)

    def read_region(self, addr: int, size: int) -> Optional[bytes]:
        """尝试读取一个 region 的内容; 不可读返回 None。"""
        lib = self._lib
        buf = ctypes.c_ulonglong(0)
        cnt = ctypes.c_uint(0)
        kr = lib.vm_read(self._task, addr, size,
                         ctypes.byref(buf), ctypes.byref(cnt))
        if kr != 0:
            return None
        try:
            data = ctypes.string_at(buf, cnt.value)
        finally:
            lib.vm_deallocate(self._task, buf.value, cnt.value)
        return data

    def scan_keys(self, max_mb: int = 0) -> Dict[str, str]:
        """遍历所有 region, 提取 {salt_hex_lower: key_hex_lower}。max_mb=0 不设上限。"""
        found: Dict[str, str] = {}
        scanned = 0
        for addr, size in self.iter_regions():
            # 跳过超大映射的常见非数据区以提速(可选)
            if size <= 0:
                continue
            if max_mb and scanned + size > max_mb * 1024 * 1024:
                break
            data = self.read_region(addr, size)
            if not data:
                continue
            scanned += size
            # 在原始字节上找 96 连 hex
            for m in HEX_RE.finditer(data):
                seg = m.group()
                # 尽量从段首起按 96 对齐取 (key64+salt32)
                key = seg[:64].decode("ascii").lower()
                salt = seg[64:96].decode("ascii").lower()
                found.setdefault(salt, key)
        return found


def scan_wechat_keys(name: Optional[str] = None,
                     progress=None) -> Dict[str, str]:
    """对运行中的微信扫描, 返回 {salt_hex: key_hex}。找不到微信进程会报错。"""
    pids = find_wechat_pids(name)
    if not pids:
        raise RuntimeError("没有找到正在运行的微信(macOS WeChat)。请先打开并登录微信。")
    merged: Dict[str, str] = {}
    for pid in pids:
        if progress:
            progress(f"扫描微信进程 pid={pid} 内存…")
        try:
            reader = MachReader(pid)
        except PermissionError:
            continue  # 该进程读不到则跳过(可能非当前登录实例)
        keys = reader.scan_keys()
        merged.update(keys)
    return merged
