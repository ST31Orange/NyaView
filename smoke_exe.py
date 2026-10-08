"""EXE smoke test: launch a built NyaView.exe and verify the page actually loads.

Catches the "window opens but page 404s" class of packaging bugs, which
process-spawn checks cannot.

Usage:
    uv run python smoke_exe.py <path-to-exe> [--wait 20]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import urllib.request


def find_listener_port(pid: int) -> int | None:
    """Find the localhost LISTENING port owned by pid or its direct children.

    PyInstaller 6 onefile builds use a two-process model: the bootloader parent
    spawns a child that runs the actual app and owns the HTTP server socket.
    """
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, errors="replace", timeout=15
        ).stdout
    except Exception:
        return None

    # owner pid -> localhost port
    candidates: dict[int, int] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and "LISTENING" in line and "127.0.0.1" in line:
            try:
                owner = int(parts[-1])
                port = int(parts[1].rsplit(":", 1)[-1])
                candidates.setdefault(owner, port)
            except ValueError:
                continue
    if pid in candidates:
        return candidates[pid]

    # onefile: the child owns the listener. Find children via Toolhelp32.
    try:
        import ctypes as ct

        TH32CS_SNAPPROCESS = 0x00000002
        INVALID_HANDLE_VALUE = ct.c_void_p(-1).value

        class PROCESSENTRY32(ct.Structure):
            _fields_ = [
                ("dwSize", ct.c_ulong),
                ("cntUsage", ct.c_ulong),
                ("th32ProcessID", ct.c_ulong),
                ("th32DefaultHeapID", ct.c_void_p),
                ("th32ModuleID", ct.c_ulong),
                ("cntThreads", ct.c_ulong),
                ("th32ParentProcessID", ct.c_ulong),
                ("pcPriClassBase", ct.c_long),
                ("dwFlags", ct.c_ulong),
                ("szExeFile", ct.c_char * 260),
            ]

        kernel32 = ct.windll.kernel32
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == INVALID_HANDLE_VALUE:
            return None
        try:
            entry = PROCESSENTRY32()
            entry.dwSize = ct.sizeof(PROCESSENTRY32)
            children = set()
            if kernel32.Process32First(snapshot, ct.byref(entry)):
                while True:
                    if entry.th32ParentProcessID == pid:
                        children.add(entry.th32ProcessID)
                    if not kernel32.Process32Next(snapshot, ct.byref(entry)):
                        break
        finally:
            kernel32.CloseHandle(snapshot)
        for child in children:
            if child in candidates:
                return candidates[child]
        return None
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("exe")
    parser.add_argument("--wait", type=int, default=25)
    args = parser.parse_args()

    proc = subprocess.Popen([args.exe])
    try:
        port = None
        deadline = time.time() + args.wait
        while time.time() < deadline:
            if proc.poll() is not None:
                print(f"FAIL: exe exited early with code {proc.returncode}")
                return 1
            port = find_listener_port(proc.pid)
            if port:
                break
            time.sleep(1)

        if not port:
            print(f"FAIL: no HTTP listener found for pid {proc.pid}")
            return 1
        print(f"listener on port {port}")

        # fetch the served page
        url = f"http://127.0.0.1:{port}/index.html"
        with urllib.request.urlopen(url, timeout=8) as resp:
            raw = resp.read()
            body = raw.decode("utf-8", errors="replace")
            status = resp.status
        if status != 200:
            print(f"FAIL: index.html returned HTTP {status}")
            return 1
        # The page must be the real NyaView UI (pywebviewready is in app.js,
        # so index.html should at least reference app.js and style.css).
        if "NyaView" not in body or 'src="app.js"' not in body or 'href="style.css"' not in body:
            print(f"FAIL: index.html missing expected markers ({len(raw)} bytes)")
            return 1
        # app.js must also be served (the bridge/boot code).
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/app.js", timeout=8) as resp_js:
            js = resp_js.read().decode("utf-8", errors="replace")
        if "pywebviewready" not in js:
            print("FAIL: app.js missing pywebviewready")
            return 1
        print(f"PASS: HTTP {status}, index.html {len(raw)} bytes, app.js {len(js)} bytes — content verified")
        return 0
    finally:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        # onefile leaves a child that owns the socket; kill it too via taskkill tree
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
        )


if __name__ == "__main__":
    sys.exit(main())
