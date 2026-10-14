"""Desktop entry and ownership of the local server's child processes."""
from __future__ import annotations

import ctypes
import os
import shutil
import signal
import subprocess
import sys
import webbrowser
from pathlib import Path

_job = None


def own_child_processes():
    """Closing the Windows console also terminates MCP and simulated IOC children."""
    global _job
    if sys.platform != "win32" or _job is not None:
        return
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                    ("flags", wintypes.DWORD), ("minimum", ctypes.c_size_t),
                    ("maximum", ctypes.c_size_t), ("active", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                    ("scheduling", wintypes.DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", BasicLimits), ("io", ctypes.c_uint64 * 6),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                    ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        error = ctypes.get_last_error()
        kernel.CloseHandle(handle)
        raise ctypes.WinError(error)
    # Keep the non-inheritable handle open for the lifetime of this server.
    _job = handle


def worker_arguments(arguments):
    prefix = ["--epics-worker"] if getattr(sys, "frozen", False) else ["-m", "epics_field_client.worker"]
    return prefix + list(arguments)


def protect_worker_parent():
    if sys.platform.startswith("linux"):
        parent = os.getppid()
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
            raise OSError(ctypes.get_errno(), "Cannot bind worker lifetime to server")
        if parent == 1 or os.getppid() != parent:
            raise SystemExit(0)


def default_data_dir():
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "EPICS Field Client"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "epics-field-client"


def system_environment():
    env = dict(os.environ)
    if getattr(sys, "frozen", False) and sys.platform.startswith("linux"):
        original = env.get("LD_LIBRARY_PATH_ORIG")
        if original is None:
            env.pop("LD_LIBRARY_PATH", None)
        else:
            env["LD_LIBRARY_PATH"] = original
    return env


def open_browser(url):
    # System browsers must not load the portable app's private native libraries.
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
        kernel.SetDllDirectoryW(None)
        try:
            return webbrowser.open(url)
        finally:
            kernel.SetDllDirectoryW(sys._MEIPASS)
    current = os.environ.get("LD_LIBRARY_PATH")
    env = system_environment()
    try:
        if "LD_LIBRARY_PATH" in env:
            os.environ["LD_LIBRARY_PATH"] = env["LD_LIBRARY_PATH"]
        else:
            os.environ.pop("LD_LIBRARY_PATH", None)
        return webbrowser.open(url)
    finally:
        if current is None:
            os.environ.pop("LD_LIBRARY_PATH", None)
        else:
            os.environ["LD_LIBRARY_PATH"] = current


def open_terminal(arguments):
    """Use a real terminal on Linux when a file manager launches the application."""
    # A venv interpreter is often a symlink; resolving it loses the venv.
    executable = Path(os.path.abspath(sys.executable))
    command = [str(executable), *arguments] if getattr(sys, "frozen", False) else [str(executable), "-m", "epics_field_client", *arguments]
    terminals = [("gnome-terminal", ["--wait", "--"]), ("konsole", ["--nofork", "-e"]),
                 ("xfce4-terminal", ["--disable-server", "-x"]), ("mate-terminal", ["--disable-factory", "-x"]),
                 ("x-terminal-emulator", ["-e"]), ("xterm", ["-e"])]
    for terminal, flags in terminals:
        path = shutil.which(terminal)
        if path:
            return subprocess.call([path, *flags, *command], env=system_environment())
    raise RuntimeError("未找到桌面终端。请安装系统的终端应用，或在现有终端运行本程序。")


def desktop_main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    arguments = sys.argv[1:]
    if arguments and arguments[0] == "--epics-worker":
        from .worker import main
        main(arguments[1:])
        return
    if sys.platform.startswith("linux") and not sys.stdin.isatty() and "--no-browser" not in arguments and "--help" not in arguments:
        try:
            raise SystemExit(open_terminal(arguments))
        except (OSError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(1)
    own_child_processes()
    from .app import main
    main()
