"""检查「当前进程所在的环境」是否允许 spawn 带管道的子进程，以及是否被注入了拦截 DLL。

背景：Rust `std::process::Command` 的 `stdin(Stdio::piped())` 走的是
「管道的可读端由**客户端打开**」这条路径（见 std 的 `anon_pipe(ours_readable=false)`）。
若环境中存在拦截命名管道「读方向」打开的注入层，这条路径会返回
`STATUS_PIPE_BUSY` → `os error 231`，表现为 spawn 失败，**而项目代码完全正常**。
本项目已在 WorkBuddy CLI 沙箱的 `tsbx.dll` 上实测到该现象。

用法：
    python tools/probe_spawn_env.py            # 查自身进程
    python tools/probe_spawn_env.py <pid>      # 查指定进程的注入情况

判读：
  · 第 1 节列出非系统模块 —— 出现沙箱/安全软件 DLL 即「进程内注入」。
  · 第 2 节两组管道方向若**只**在「客户端读」那组失败，即为命中该拦截。
  · 第 3 节给结论。跨环境对照（工具沙箱内 vs 用户自己的终端）是最终判据。
"""

import ctypes
import ctypes.wintypes as w
import os
import sys
import uuid

# ---------- 第 1 节：模块清单 ----------

psapi = ctypes.WinDLL("psapi", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
LIST_MODULES_ALL = 0x03
INVALID = ctypes.c_void_p(-1).value

psapi.EnumProcessModulesEx.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                                       w.DWORD, ctypes.POINTER(w.DWORD), w.DWORD]
psapi.GetModuleFileNameExW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, w.LPWSTR, w.DWORD]
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
kernel32.GetCurrentProcess.restype = ctypes.c_void_p
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

SUSPECT = ("sandbox", "tsbx", "hook", "inject", "anticheat", "ace-", "guard", "detour",
           "minhook", "easyhook", "360", "huorong", "hrsword", "qqpcmgr")

SYSTEM_PREFIXES = ("c:\\windows\\system32", "c:\\windows\\syswow64", "c:\\windows\\winsxs")


def list_modules(pid=None):
    """返回 (模块路径列表, 错误信息)。pid 为 None 时查自身。"""
    own = pid is None
    if own:
        h = kernel32.GetCurrentProcess()
    else:
        h = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if h in (None, INVALID):
            return [], f"OpenProcess 失败（err={ctypes.get_last_error()}，多为权限不足）"
    try:
        need = w.DWORD()
        arr = (ctypes.c_void_p * 4096)()
        if not psapi.EnumProcessModulesEx(h, arr, ctypes.sizeof(arr), ctypes.byref(need),
                                          LIST_MODULES_ALL):
            return [], f"EnumProcessModulesEx 失败（err={ctypes.get_last_error()}）"
        n = need.value // ctypes.sizeof(ctypes.c_void_p)
        buf = ctypes.create_unicode_buffer(1024)
        out = []
        for i in range(min(n, 4096)):
            if psapi.GetModuleFileNameExW(h, arr[i], buf, 1024) and buf.value:
                out.append(buf.value)
        return out, None
    finally:
        if not own:
            kernel32.CloseHandle(h)


def section_modules(pid):
    print(f"=== 1. 模块清单（{'自身' if pid is None else f'pid={pid}'}）===")
    mods, err = list_modules(pid)
    if err:
        print(f"  {err}")
        return False
    non_sys = [m for m in mods if not m.lower().startswith(SYSTEM_PREFIXES)]
    print(f"  共 {len(mods)} 个模块，其中非系统目录 {len(non_sys)} 个：")
    injected = []
    for m in sorted(non_sys):
        low = m.lower()
        if any(s in low for s in SUSPECT):
            injected.append(m)
            print(f"    [!] {m}   <== 疑似注入的拦截 DLL")
        else:
            print(f"        {m}")
    if injected:
        print(f"\n  ⇒ 检出 {len(injected)} 个疑似注入模块。若进程本该「干净」，这就是拦截者。")
    else:
        print("\n  ⇒ 未检出明显的注入模块。")
    return bool(injected)


# ---------- 第 2 节：命名管道方向自测 ----------

PIPE_ACCESS_INBOUND = 0x00000001
PIPE_ACCESS_OUTBOUND = 0x00000002
PIPE_TYPE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
INVALID_H = ctypes.c_void_p(-1).value

kernel32.CreateNamedPipeW.restype = ctypes.c_void_p
kernel32.CreateNamedPipeW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.DWORD,
                                      w.DWORD, w.DWORD, w.DWORD, ctypes.c_void_p]
kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                                 w.DWORD, w.DWORD, ctypes.c_void_p]


def pipe_direction(server_mode, client_access):
    """建一条命名管道并让客户端按指定方向打开。返回 (ok, err)。"""
    name = r"\\.\pipe\_spawnenv_" + uuid.uuid4().hex[:10]
    hs = kernel32.CreateNamedPipeW(name, server_mode, PIPE_TYPE_BYTE | PIPE_WAIT,
                                   1, 65536, 65536, 0, None)
    if hs == INVALID_H:
        return False, ctypes.get_last_error()
    try:
        ctypes.set_last_error(0)
        hc = kernel32.CreateFileW(name, client_access, 0, None, OPEN_EXISTING, 0, None)
        err = ctypes.get_last_error()
        if hc != INVALID_H:
            kernel32.CloseHandle(hc)
            return True, 0
        return False, err
    finally:
        kernel32.CloseHandle(hs)


def section_pipes():
    print()
    print("=== 2. 命名管道方向自测 ===")
    cases = [
        ("服务端只写 + 客户端**读**（Rust stdin piped 走这条）", PIPE_ACCESS_OUTBOUND, GENERIC_READ),
        ("服务端只读 + 客户端写（Rust stdout piped 走这条）    ", PIPE_ACCESS_INBOUND, GENERIC_WRITE),
    ]
    results = {}
    for label, mode, acc in cases:
        ok, err = pipe_direction(mode, acc)
        results[label] = ok
        print(f"  {label}: {'OK' if ok else f'FAIL err={err}'}")
    return results


def main():
    pid = int(sys.argv[1]) if len(sys.argv) > 1 else None
    print(f"进程: {sys.executable}")
    print()
    injected = section_modules(pid)
    r = section_pipes()
    print()
    print("=== 3. 结论 ===")
    read_ok = list(r.values())[0]
    write_ok = list(r.values())[1]
    if read_ok and write_ok:
        print("  两个方向都通 ⇒ 本环境可以正常 spawn 带 stdin 管道的子进程。")
    elif not read_ok and write_ok:
        print("  「客户端读」被拒、「客户端写」放行 ⇒ **命中已知的管道读拦截**。")
        print("  表现：Rust 的 stdin(Stdio::piped()) / tauri dev / app-ui 起 worker 都会 os error 231。")
        if injected:
            print("  且已检出注入模块（见第 1 节）⇒ 拦截由该注入层实现。")
        print("  处置：**换一个不在该注入层进程树内的终端**跑一次做对照；这不是项目代码的问题。")
    else:
        print("  两个方向都不通 ⇒ 另有原因，本脚本的判读不适用。")
    print()


if __name__ == "__main__":
    main()
