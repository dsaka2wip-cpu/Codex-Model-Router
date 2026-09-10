"""Real backend bridge recovery check. Never starts a turn or creates a thread."""

import ctypes
import json
import os
import time
from ctypes import wintypes
from pathlib import Path

from app_server import AppServer


ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "state" / "native-runtime.json"
REPORT = ROOT / "state" / "native-recovery-check.json"
TH32CS_SNAPPROCESS = 0x00000002
SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
    ]


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = wintypes.BOOL
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.restype = wintypes.BOOL
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.TerminateProcess.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


def children_of(parent_pid):
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        return []
    children = []
    try:
        entry = PROCESSENTRY32W(dwSize=ctypes.sizeof(PROCESSENTRY32W))
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.th32ParentProcessID == parent_pid:
                children.append(entry.th32ProcessID)
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return children


def open_verified_child(parent_pid, expected_image, timeout=5):
    deadline = time.monotonic() + timeout
    expected = os.path.normcase(os.path.realpath(expected_image))
    while time.monotonic() < deadline:
        matches = []
        for pid in children_of(parent_pid):
            handle = kernel32.OpenProcess(
                SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE, False, pid)
            if not handle:
                continue
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if (kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)) and
                    os.path.normcase(os.path.realpath(buffer.value)) == expected):
                matches.append(handle)
            else:
                kernel32.CloseHandle(handle)
        if len(matches) == 1:
            return matches[0]
        for handle in matches:
            kernel32.CloseHandle(handle)
        time.sleep(0.1)
    return None


def connect(runtime):
    return AppServer(
        cwd=str(ROOT),
        executable=runtime["python"],
        arguments=["-u", "-B", str(ROOT / "stdio_router.py"), "app-server", "--stdio"],
    )


def safe_snapshot(server):
    account = (server.call("account/read", {"refreshToken": False}) or {}).get("account") or {}
    listed = server.call("thread/list", {"limit": 20, "useStateDbOnly": True}) or {}
    rows = listed.get("data")
    if not isinstance(rows, list):
        raise RuntimeError("invalid_thread_list")
    ids = {row.get("id") for row in rows
           if isinstance(row, dict) and isinstance(row.get("id"), str)}
    return account.get("type") == "chatgpt" and account.get("planType") == "pro", ids


def stop_owned_bridge(server, child):
    """Terminate the owned Python bridge, then wait for its verified child to see EOF."""
    if server.process.poll() is None:
        server.process.terminate()
    try:
        server.process.wait(timeout=5)
    except Exception:
        server.process.kill()
        server.process.wait(timeout=5)
    child_exited = kernel32.WaitForSingleObject(child, 10000) == WAIT_OBJECT_0
    forced_cleanup = False
    if not child_exited:
        kernel32.TerminateProcess(child, 1)
        forced_cleanup = kernel32.WaitForSingleObject(child, 5000) == WAIT_OBJECT_0
    return child_exited, forced_cleanup


def main():
    runtime = json.loads(RUNTIME.read_text(encoding="utf-8-sig"))
    checks = {}
    first = second = first_child = second_child = None
    forced_child_cleanup = False
    failure_stage = None
    before_ids = after_ids = set()
    try:
        first = connect(runtime)
        checks["initial_auth"], before_ids = safe_snapshot(first)
        checks["initial_list"] = len(before_ids) <= 20
        first_child = open_verified_child(first.process.pid, runtime["real_codex"])
        checks["owned_child_identified"] = first_child is not None
        if first_child is None:
            raise RuntimeError("owned_child_not_identified")

        # Only this check's Python bridge is terminated. Its exact child handle is already held.
        checks["child_exited_on_eof"], forced = stop_owned_bridge(first, first_child)
        forced_child_cleanup |= forced
        checks["owned_bridge_terminated"] = True

        second = connect(runtime)
        checks["reconnected_auth"], after_ids = safe_snapshot(second)
        checks["reconnected_list"] = len(after_ids) <= 20
        checks["prior_threads_visible"] = before_ids.issubset(after_ids)
        second_child = open_verified_child(second.process.pid, runtime["real_codex"])
        checks["reconnected_child_identified"] = second_child is not None
        if second_child is None:
            raise RuntimeError("owned_child_not_identified")
        checks["reconnected_child_exited_on_eof"], forced = stop_owned_bridge(second, second_child)
        forced_child_cleanup |= forced
    except Exception as exc:
        failure_stage = str(exc) if str(exc) in {"owned_child_not_identified"} else type(exc).__name__
    finally:
        if first is not None:
            first.close()
        if second is not None:
            second.close()
        for child in (first_child, second_child):
            if child is not None:
                if kernel32.WaitForSingleObject(child, 0) == WAIT_TIMEOUT:
                    kernel32.TerminateProcess(child, 1)
                    forced_child_cleanup |= kernel32.WaitForSingleObject(child, 5000) == WAIT_OBJECT_0
                kernel32.CloseHandle(child)

    passed = bool(checks) and all(checks.values())
    report = {
        "kind": "backend_protocol_recovery_check",
        "gui_checked": False,
        "passed": passed,
        "checks": checks,
        "counts": {
            "checks_passed": sum(value is True for value in checks.values()),
            "checks_total": len(checks),
            "threads_before": len(before_ids),
            "threads_after": len(after_ids),
            "threads_retained": len(before_ids & after_ids),
        },
        "forced_owned_child_cleanup": forced_child_cleanup,
        "failure_stage": failure_stage,
        "model_calls": 0,
        "turn_calls": 0,
        "threads_created": 0,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": passed, **report["counts"]}, separators=(",", ":")))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
