import os
import shutil
import sys
import threading

from common import APP, VERSION

DONE = "ready.txt"


def root():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, APP, "app")


def is_onefile():
    meipass = getattr(sys, "_MEIPASS", "")
    return bool(getattr(sys, "frozen", False) and os.path.basename(meipass.rstrip("\\/")).startswith("_MEI"))


def is_fast():
    return bool(getattr(sys, "frozen", False) and not is_onefile()
                and os.path.normcase(os.path.abspath(sys.executable)).startswith(os.path.normcase(root()) + os.sep))


def folder(version=VERSION):
    return os.path.join(root(), version)


def exe(version=VERSION):
    path = os.path.join(folder(version), "AnyKir.exe")
    return path if os.path.isfile(path) and os.path.isfile(os.path.join(folder(version), DONE)) else None


def home_exe():
    if is_onefile():
        return sys.executable
    try:
        with open(os.path.join(os.path.dirname(sys.executable), DONE), encoding="utf-8") as f:
            path = f.read().strip()
        return path if path and os.path.isfile(path) else None
    except OSError:
        return None


def launcher():
    if is_onefile():
        return exe() or sys.executable
    return sys.executable


def ensure():
    if not is_onefile() or exe():
        return exe()
    launcher_src = os.path.join(sys._MEIPASS, "svc", "AnyKirSvc.exe")
    if not os.path.isfile(launcher_src):
        return None
    dest = folder()
    part = dest + ".part"
    try:
        shutil.rmtree(part, ignore_errors=True)
        shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(part)
        shutil.copytree(sys._MEIPASS, os.path.join(part, "_internal"))
        shutil.copy2(launcher_src, os.path.join(part, "AnyKir.exe"))
        with open(os.path.join(part, DONE), "w", encoding="utf-8") as f:
            f.write(sys.executable)
        os.replace(part, dest)
    except OSError:
        shutil.rmtree(part, ignore_errors=True)
        return None
    _remove_other_versions()
    return exe()


def _remove_other_versions():
    try:
        names = os.listdir(root())
    except OSError:
        return
    for name in names:
        if name == VERSION:
            continue
        path = os.path.join(root(), name)
        if _in_use(path):
            continue
        shutil.rmtree(path, ignore_errors=True)


def _in_use(path):
    internal = os.path.join(path, "_internal")
    try:
        dlls = [n for n in os.listdir(internal) if n.lower().startswith("python3") and n.lower().endswith(".dll")]
    except OSError:
        return False
    for name in dlls:
        try:
            os.remove(os.path.join(internal, name))
        except FileNotFoundError:
            pass
        except OSError:
            return True
    return False


def start_menu_shortcut(target):
    try:
        import pythoncom
        from win32com.client import Dispatch
        pythoncom.CoInitialize()
        programs = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu", "Programs")
        link = Dispatch("WScript.Shell").CreateShortcut(os.path.join(programs, f"{APP}.lnk"))
        link.TargetPath = target
        link.WorkingDirectory = os.path.dirname(target)
        link.IconLocation = f"{target},0"
        link.Description = "AnyKir remote desktop"
        link.Save()
        return True
    except Exception:
        return False


def prepare_in_background(on_ready=None, delay=8):
    if not is_onefile():
        return

    def work():
        import time
        time.sleep(delay)
        target = ensure()
        if target:
            start_menu_shortcut(target)
            if on_ready:
                on_ready(target)
    threading.Thread(target=work, daemon=True, name="fastcopy").start()
