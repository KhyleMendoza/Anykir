import os
import shutil
import sys
import tempfile
import time

MARKERS = (os.path.join("svc", "AnyKirSvc.exe"), os.path.join("svc", "AnyKirSvc.zip"), "anykir.bundle")
MIN_AGE = 10 * 60


def _old_enough(path, now):
    try:
        return now - os.path.getmtime(path) >= MIN_AGE
    except OSError:
        return False


def _remove_unused(path):
    doomed = f"{path}.del-{os.getpid()}"
    try:
        os.rename(path, doomed)
    except OSError:
        return 0
    size = folder_size(doomed)
    shutil.rmtree(doomed, ignore_errors=True)
    return size


def folder_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def unpacked_copies(temp_dir):
    current = os.path.normcase(os.path.abspath(getattr(sys, "_MEIPASS", "") or "-"))
    try:
        names = os.listdir(temp_dir)
    except OSError:
        return []
    found = []
    for name in names:
        full = os.path.join(temp_dir, name)
        if not name.startswith("_MEI") or os.path.normcase(os.path.abspath(full)) == current:
            continue
        if os.path.isdir(full) and any(os.path.exists(os.path.join(full, m)) for m in MARKERS):
            found.append(full)
    return found


def clean_temp(temp_dirs=None, now=None):
    now = time.time() if now is None else now
    freed = 0
    for temp_dir in temp_dirs or [tempfile.gettempdir()]:
        for path in unpacked_copies(temp_dir):
            if _old_enough(path, now):
                freed += _remove_unused(path)
        try:
            for name in os.listdir(temp_dir):
                if name.startswith("_MEI") and ".del-" in name:
                    shutil.rmtree(os.path.join(temp_dir, name), ignore_errors=True)
        except OSError:
            pass
    return freed


def system_temp_dirs():
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return [p for p in (os.path.join(root, "SystemTemp"), os.path.join(root, "Temp")) if os.path.isdir(p)]


def clean_setup_leftovers(now=None):
    now = time.time() if now is None else now
    root = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Temp")
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        full = os.path.join(root, name)
        if name.startswith("AnyKirSetup-") and _old_enough(full, now):
            shutil.rmtree(full, ignore_errors=True)


def clean_in_background():
    import threading
    threading.Thread(target=lambda: clean_temp(), daemon=True).start()
