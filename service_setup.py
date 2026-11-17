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
        return os.path.join(sys._MEIPASS, "svc", "AnyKirSvc.exe")
    return os.path.join(HERE, "build_all", "svc", "dist", "AnyKirSvc")


def stage_service(staging):
    payload = _bundle()
    source = os.path.join(staging, "AnyKirSvc")
    if os.path.isdir(payload):
        shutil.copytree(payload, source)
    elif os.path.isfile(payload):
        os.makedirs(source)
        shutil.copy2(payload, os.path.join(source, "AnyKirSvc.exe"))
        shutil.copytree(sys._MEIPASS, os.path.join(source, "_internal"),
                        ignore=lambda folder, names: ["svc"] if os.path.samefile(folder, sys._MEIPASS) else [])
    elif os.path.isfile(os.path.join(os.path.dirname(payload), "AnyKirSvc.zip")):
        with zipfile.ZipFile(os.path.join(os.path.dirname(payload), "AnyKirSvc.zip")) as z:
            z.extractall(staging)
    else:
        return None
    return source


def available():
    return sys.platform == "win32" and (os.path.exists(_bundle()) or os.path.exists(
        os.path.join(os.path.dirname(_bundle()), "AnyKirSvc.zip")))


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
    if action in ("free-port", "allow-firewall"):
        args = [f"--{action}", str(int(port))]
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


INSTALL_SDDL = "O:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;BU)"
DATA_SDDL = "O:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"


def _read_json(path):
    import json
    try:
        with open(path, "rb") as f:
            data = json.loads(f.read(65536).decode("utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def service_config(existing, app, follow, port=7001):
    import re
    cfg = {"id": str(secrets.randbelow(900000000) + 100000000), "secret": secrets.token_urlsafe(32),
           "password": secrets.token_urlsafe(18), "listen": port, "allow_input": True,
           "allow_update": False, "allow_files": False, "allow_clipboard": True, "follow": existing.get("follow", "")}
    for source in (existing, app):
        if re.fullmatch(r"\d{6,12}", str(source.get("id", ""))):
            cfg["id"] = str(source["id"])
        for key, shortest in (("secret", 16), ("password", 8)):
            if isinstance(source.get(key), str) and len(source[key]) >= shortest:
                cfg[key] = source[key]
    if existing.get("allow_input") is False:
        cfg["allow_input"] = False
    for key in ("allow_input", "allow_clipboard"):
        if isinstance(app.get(key), bool):
            cfg[key] = app[key]
    if isinstance(app.get("listen"), int) and not isinstance(app["listen"], bool) and 0 < app["listen"] < 65536:
        cfg["listen"] = app["listen"]
    if follow:
        cfg["follow"] = os.path.abspath(follow)
    return cfg


def _no_reparse_points(path):
    path = os.path.abspath(path)
    while True:
        if os.path.lexists(path) and (os.path.islink(path) or ctypes.windll.kernel32.GetFileAttributesW(path) & 0x400):
            raise RuntimeError(f"Refusing a service path containing a link: {path}")
        parent = os.path.dirname(path)
        if parent == path:
            return
        path = parent


def _protect(path, sddl):
    import win32security
    _no_reparse_points(path)
    os.makedirs(path, exist_ok=True)
    sd = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(sddl, win32security.SDDL_REVISION_1)
    win32security.SetNamedSecurityInfo(
        path, win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION
        | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        sd.GetSecurityDescriptorOwner(), None, sd.GetSecurityDescriptorDacl(), None)


def _empty_folder(path):
    import time
    for _ in range(10):
        try:
            for name in os.listdir(path):
                full = os.path.join(path, name)
                if os.path.isdir(full) and not os.path.islink(full):
                    shutil.rmtree(full)
                else:
                    os.remove(full)
            return
        except OSError:
            time.sleep(1)
    raise RuntimeError(f"Could not clear {path}: a file there is still in use.")


def _register_service(exe):
    import win32service
    import win32serviceutil
    bin_path = f'"{exe}" --service'
    scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ALL_ACCESS)
    try:
        try:
            h = win32service.OpenService(scm, SERVICE_NAME, win32service.SERVICE_ALL_ACCESS)
            win32service.ChangeServiceConfig(
                h, win32service.SERVICE_WIN32_OWN_PROCESS, win32service.SERVICE_AUTO_START,
                win32service.SERVICE_ERROR_NORMAL, bin_path, None, 0, None, "LocalSystem", None,
                "AnyKir administrator access")
        except win32service.error as e:
            if e.winerror != 1060:
                raise
            h = win32service.CreateService(
                scm, SERVICE_NAME, "AnyKir administrator access", win32service.SERVICE_ALL_ACCESS,
                win32service.SERVICE_WIN32_OWN_PROCESS, win32service.SERVICE_AUTO_START,
                win32service.SERVICE_ERROR_NORMAL, bin_path, None, 0, None, None, None)
        try:
            win32service.ChangeServiceConfig2(h, win32service.SERVICE_CONFIG_DESCRIPTION,
                                              "Remote screen and input for administrator windows and UAC prompts.")
            win32service.ChangeServiceConfig2(h, win32service.SERVICE_CONFIG_FAILURE_ACTIONS, {
                "ResetPeriod": 86400, "RebootMsg": None, "Command": None,
                "Actions": [(win32service.SC_ACTION_RESTART, 5000), (win32service.SC_ACTION_RESTART, 15000),
                            (win32service.SC_ACTION_RESTART, 60000)]})
        finally:
            win32service.CloseServiceHandle(h)
    finally:
        win32service.CloseServiceHandle(scm)
    try:
        win32serviceutil.StartService(SERVICE_NAME)
        win32serviceutil.WaitForServiceStatus(SERVICE_NAME, win32service.SERVICE_RUNNING, 30)
    except Exception:
        import time
        kill_service_processes()
        time.sleep(2)
        win32serviceutil.StartService(SERVICE_NAME)
        win32serviceutil.WaitForServiceStatus(SERVICE_NAME, win32service.SERVICE_RUNNING, 60)


def kill_service_processes():
    import subprocess
    taskkill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
    try:
        subprocess.run([taskkill, "/F", "/IM", "AnyKirSvc.exe"], capture_output=True,
                       creationflags=0x08000000, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def _clean_leftovers():
    import cleanup
    cleanup.clean_setup_leftovers()
    cleanup.clean_temp(cleanup.system_temp_dirs())


def install(host_config=None):
    import json
    import windows_service
    staging = _protected_dir()
    try:
        source = stage_service(staging)
        if not source or not os.path.isfile(os.path.join(source, "AnyKirSvc.exe")):
            return False, "The service build inside this AnyKir.exe is incomplete. Rebuild with build.bat."
        install_dir, data_dir = windows_service.install_dir(), windows_service.data_dir()
        _no_reparse_points(install_dir)
        _no_reparse_points(data_dir)
        if state() is not None:
            try:
                stop_service()
            except Exception:
                pass
        kill_service_processes()
        _clean_leftovers()
        _protect(install_dir, INSTALL_SDDL)
        _protect(data_dir, DATA_SDDL)
        _empty_folder(install_dir)
        shutil.copytree(source, install_dir, dirs_exist_ok=True)

        config = os.path.join(data_dir, "host.json")
        app_file = host_config if host_config and os.path.isfile(host_config) else None
        cfg = service_config(_read_json(config), _read_json(app_file) if app_file else {}, app_file)
        if os.path.lexists(config):
            os.remove(config)
        with open(config, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)

        _register_service(os.path.join(install_dir, "AnyKirSvc.exe"))
        firewall = windows_service.open_firewall(cfg["listen"])
        text = (f"AnyKir admin service installed and started on port {cfg['listen']}.\n"
                "It uses the same address and password as the AnyKir window and follows its changes.\n"
                "Copy and paste works through the service; file transfer needs the AnyKir window.")
        if not firewall:
            text += "\n\nWarning: the Windows Firewall rule could not be added."
        return True, text
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def uninstall():
    import win32serviceutil
    import windows_service
    was_installed = state() is not None
    if was_installed:
        try:
            stop_service()
        except Exception:
            pass
        win32serviceutil.RemoveService(SERVICE_NAME)
    kill_service_processes()
    install_dir, data_dir = windows_service.install_dir(), windows_service.data_dir()
    freed = 0
    import cleanup
    for path in (install_dir, os.path.join(data_dir, "update")):
        if os.path.isdir(path) and not os.path.islink(path):
            freed += cleanup.folder_size(path)
            shutil.rmtree(path, ignore_errors=True)
    for name in ("update.log", "update_attempt.json", "version.txt", "follow.txt"):
        try:
            os.remove(os.path.join(data_dir, name))
        except OSError:
            pass
    _clean_leftovers()
    left = os.path.isdir(install_dir) and os.listdir(install_dir)
    text = ("The administrator service was removed" if was_installed else "The administrator service was not installed")
    text += f" and {freed / 1048576:.0f} MB of its files were deleted."
    if left:
        text += f"\nSome files in {install_dir} are still in use; they go after a restart."
    return True, text


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


def follows_this_app():
    import windows_service
    follow = windows_service.installed_follow()
    mine = os.path.join(config_dir(), "host.json")
    return bool(follow) and os.path.normcase(os.path.abspath(follow)) == os.path.normcase(os.path.abspath(mine))


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
        action = "firewall" if "--allow-firewall" in args else "free-port"
        try:
            port = int(value("--" + ("allow-firewall" if action == "firewall" else "free-port")))
        except (TypeError, ValueError):
            sys.exit(2)
    host_config = value("--host-config")

    root = tk.Tk()
    try:
        import logo
        logo.tk_icon(root)
    except Exception:
        pass
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
            "free-port": "Freeing the port...", "firewall": "Allowing AnyKir through Windows Firewall..."}[action]
    ttk.Label(root, text=text, padding=24).pack()
    result = {}

    def work():
        try:
            if action == "install":
                result["ok"], result["text"] = install(host_config)
            elif action == "uninstall":
                result["ok"], result["text"] = uninstall()
            elif action == "firewall":
                import windows_service
                ok = windows_service.open_firewall(port)
                result["ok"], result["text"] = ok, "" if ok else "Windows Firewall refused the AnyKir rule."
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
        if result["ok"] and action in ("free-port", "firewall"):
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
