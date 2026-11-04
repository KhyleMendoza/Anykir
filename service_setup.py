import ctypes
import os
import secrets
import shutil
import subprocess
import sys
import zipfile

from common import APP, config_dir

SERVICE_NAME = "AnyKir"
HERE = os.path.dirname(os.path.abspath(__file__))
CREATE_NO_WINDOW = 0x08000000


def _bundle():
    if getattr(sys, "frozen", False):
        base = os.path.join(sys._MEIPASS, "svc")
        return os.path.join(base, "AnyKirSvc.zip"), os.path.join(base, "install-service.ps1")
    return (os.path.join(HERE, "build_all", "svc", "dist", "AnyKirSvc"),
            os.path.join(HERE, "install-service.ps1"))


def available():
    if sys.platform != "win32":
        return False
    payload, script = _bundle()
    return os.path.exists(payload) and os.path.isfile(script)


def state():
    try:
        import win32service
        import win32serviceutil
        status = win32serviceutil.QueryServiceStatus(SERVICE_NAME)[1]
    except Exception:
        return None
    return "running" if status == win32service.SERVICE_RUNNING else "stopped"


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("fMask", ctypes.c_ulong), ("hwnd", ctypes.c_void_p),
                ("lpVerb", ctypes.c_wchar_p), ("lpFile", ctypes.c_wchar_p), ("lpParameters", ctypes.c_wchar_p),
                ("lpDirectory", ctypes.c_wchar_p), ("nShow", ctypes.c_int), ("hInstApp", ctypes.c_void_p),
                ("lpIDList", ctypes.c_void_p), ("lpClass", ctypes.c_wchar_p), ("hkeyClass", ctypes.c_void_p),
                ("dwHotKey", ctypes.c_ulong), ("hIcon", ctypes.c_void_p), ("hProcess", ctypes.c_void_p)]


def run_elevated(action, port=None):
    if action == "free-port":
        args = ["--free-port", str(int(port))]
    else:
        args = [f"--{action}-service", "--host-config", os.path.join(config_dir(), "host.json")]
    if getattr(sys, "frozen", False):
        exe = sys.executable
    else:
        exe = sys.executable.replace("python.exe", "pythonw.exe")
        args.insert(0, os.path.join(HERE, "anykir.py"))
    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040 | 0x00000400
    info.lpVerb, info.lpFile = "runas", exe
    info.lpParameters = subprocess.list2cmdline(args)
    info.nShow = 1
    old = os.environ.get("PYINSTALLER_RESET_ENVIRONMENT")
    os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    try:
        ok = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info))
    finally:
        if old is None:
            os.environ.pop("PYINSTALLER_RESET_ENVIRONMENT", None)
        else:
            os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = old
    if not ok or not info.hProcess:
        return None
    k = ctypes.windll.kernel32
    k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    k.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    try:
        k.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)
        code = ctypes.c_ulong()
        k.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return code.value
    finally:
        k.CloseHandle(info.hProcess)


def _protected_dir():
    import win32file
    import win32security
    sa = win32security.SECURITY_ATTRIBUTES()
    sa.SECURITY_DESCRIPTOR = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        "O:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)", win32security.SDDL_REVISION_1)
    path = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Temp", "AnyKirSetup-" + secrets.token_hex(8))
    win32file.CreateDirectory(path, sa)
    return path


def install(host_config=None):
    payload, script_src = _bundle()
    staging = _protected_dir()
    try:
        source = os.path.join(staging, "AnyKirSvc")
        if os.path.isdir(payload):
            shutil.copytree(payload, source)
        else:
            with zipfile.ZipFile(payload) as z:
                z.extractall(staging)
        if not os.path.isfile(os.path.join(source, "AnyKirSvc.exe")):
            return False, "The service build inside this AnyKir.exe is incomplete. Rebuild with build.bat."
        script = os.path.join(staging, "install-service.ps1")
        shutil.copy2(script_src, script)
        powershell = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                  "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        cmd = [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", script, "-Source", source]
        if host_config and os.path.isfile(host_config):
            cmd += ["-HostConfig", host_config]
        p = subprocess.run(cmd, capture_output=True, encoding="oem", errors="replace",
                           creationflags=CREATE_NO_WINDOW, timeout=300)
        return p.returncode == 0, (p.stdout + "\n" + p.stderr).strip()
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def uninstall():
    import win32serviceutil
    if state() is None:
        return True, "The administrator service is not installed."
    stop_service()
    win32serviceutil.RemoveService(SERVICE_NAME)
    return True, "The administrator service was removed. Its files and settings were kept."


def service_update(args):
    import json
    import time
    import windows_service
    if not windows_service.is_system():
        raise PermissionError("Only the installed AnyKir service starts this.")
    host_config = args[args.index("--host-config") + 1] if "--host-config" in args else None
    try:
        ok, text = install(host_config)
    except Exception as e:
        ok, text = False, f"{type(e).__name__}: {e}"
    with open(os.path.join(windows_service.data_dir(), "update.log"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "ok": ok, "out": text[-2000:]}) + chr(10))


def installed_version():
    import windows_service
    return windows_service.installed_version()


def is_service_agent(path):
    return os.path.basename(path or "").lower() == "anykirsvc.exe"


def stop_service():
    import pywintypes
    import win32service
    import win32serviceutil
    try:
        win32serviceutil.StopService(SERVICE_NAME)
    except pywintypes.error as e:
        if e.winerror != 1062:
            raise
    win32serviceutil.WaitForServiceStatus(SERVICE_NAME, win32service.SERVICE_STOPPED, 30)


def free_port(port):
    from network_info import kill_process, port_owners
    done = []
    for pid, path in port_owners(port):
        name = os.path.basename(path) or f"PID {pid}"
        if is_service_agent(path) and state() == "running":
            stop_service()
            done.append("Stopped the AnyKir admin service (it starts again with Windows unless you remove it).")
        elif kill_process(pid):
            done.append(f"Closed {name} (PID {pid}).")
        else:
            return False, f"Could not close {name} (PID {pid})."
    return True, "\n".join(done) or f"Port {port} is already free."


def main(args):
    import threading
    import tkinter as tk
    from tkinter import messagebox, ttk
    def value(flag):
        if flag in args:
            i = args.index(flag)
            return args[i + 1] if i + 1 < len(args) else None
        return None
    if "--install-service" in args:
        action = "install"
    elif "--uninstall-service" in args:
        action = "uninstall"
    else:
        action = "free-port"
        try:
            port = int(value("--free-port"))
        except (TypeError, ValueError):
            sys.exit(2)
    host_config = value("--host-config")

    root = tk.Tk()
    root.title(APP)
    root.resizable(False, False)
    if not is_admin():
        root.withdraw()
        messagebox.showerror(APP, "This needs to run as administrator.", parent=root)
        root.destroy()
        sys.exit(2)
    if action == "install" and not available():
        root.withdraw()
        messagebox.showerror(APP, "This AnyKir.exe does not contain the administrator service. "
                                  "Rebuild it with build.bat.", parent=root)
        root.destroy()
        sys.exit(2)
    text = {"install": "Installing the administrator service...", "uninstall": "Removing the administrator service...",
            "free-port": "Freeing the port..."}[action]
    ttk.Label(root, text=text, padding=24).pack()
    result = {}

    def work():
        try:
            if action == "install":
                result["ok"], result["text"] = install(host_config)
            elif action == "uninstall":
                result["ok"], result["text"] = uninstall()
            else:
                result["ok"], result["text"] = free_port(port)
        except Exception as e:
            result["ok"], result["text"] = False, f"{type(e).__name__}: {e}"

    threading.Thread(target=work, daemon=True).start()

    def check():
        if not result:
            root.after(200, check)
            return
        root.withdraw()
        if result["ok"] and action == "free-port":
            pass
        elif result["ok"]:
            head = ("Administrator service installed. Connect to this PC as usual.\n\n" if action == "install" else "")
            messagebox.showinfo(APP, head + result["text"], parent=root)
        else:
            messagebox.showerror(APP, "That did not work:\n\n" + result["text"][-1500:], parent=root)
        root.destroy()

    root.after(200, check)
    root.mainloop()
    sys.exit(0 if result.get("ok") else 1)
