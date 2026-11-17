import os
import sys

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

if __name__ == "__main__" and "--service" in sys.argv[1:]:
    import windows_service
    windows_service.dispatch()
    sys.exit(0)

import threading
import time
import traceback

import cleanup
import updater
from common import APP, VERSION, acquire_single_instance, config_dir, show_hook, single_instance, unlock


def trace(step):
    try:
        path = os.path.join(config_dir(), "start.log")
        if os.path.exists(path) and os.path.getsize(path) > 65536:
            os.replace(path, path + ".old")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{os.getpid()}] {step}\n")
    except OSError:
        pass


def error_box(text):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, APP, 0x10 | 0x40000)
    except Exception:
        pass


def dpi_aware():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


def _fast_copy_ready(target):
    trace(f"fast copy ready: {target}")
    try:
        import host
        host.refresh_autostart()
    except Exception as e:
        trace(f"autostart refresh failed: {e}")


def preload():
    try:
        import host
        import viewer
    except Exception:
        pass


def choose():
    import tkinter as tk
    import ui
    root = tk.Tk()
    try:
        import logo
        logo.tk_icon(root)
    except Exception:
        pass
    root.title(APP)
    ui.apply(root)
    picked = []
    show_hook["fn"] = lambda: root.after(0, lambda: (root.deiconify(), root.lift()))

    def pick(mode):
        picked.append(mode)
        root.destroy()

    ui.appbar(root, VERSION)
    b = ui.scroll_area(root, pady=(4, 16))
    ui.headline(b, "What do you want to do?")
    for mode, icon, title, sub in (("host", "share", "Share this PC", "Let another PC or a phone control this one"),
                                   ("viewer", "connect", "Connect to a PC",
                                    "Control one of your PCs or phones from here")):
        card = ui.ListCard(b, title, sub, lambda _c, m=mode: pick(m), icon=icon)
        card.frame.configure(padding=(16, 16, 12, 16))
    foot = ui.ttk.Frame(b)
    foot.pack(fill="x", pady=(10, 0))
    ui.ttk.Label(foot, text="Appearance", style="Label.TLabel").pack(anchor="w", pady=(0, 6))
    ui.appearance(foot).pack(anchor="w")
    ui.reveal(root)
    root.bind("1", lambda _e: pick("host"))
    root.bind("2", lambda _e: pick("viewer"))
    ui.size_to_content(root, 500, b, extra=90)
    root.mainloop()
    return picked[0] if picked else None


def main():
    args = sys.argv[1:]
    if "--service" in args:
        import windows_service
        windows_service.dispatch()
        return
    if "--service-agent" in args:
        import windows_service
        windows_service.run_agent()
        return
    if "--service-update" in args:
        import service_setup
        cleanup.clean_temp(cleanup.system_temp_dirs())
        service_setup.service_update(args)
        return
    if any(a in args for a in ("--install-service", "--uninstall-service", "--free-port", "--allow-firewall")):
        import service_setup
        service_setup.main(args)
        return
    trace(f"start v{VERSION} args={args} exe={sys.executable}")
    cleanup.clean_in_background()
    updater.startup()
    if "--nogui" not in args and not acquire_single_instance():
        if single_instance["answered"]:
            trace("another AnyKir is already running; asked it to show its window")
        else:
            trace("port 47653 is used by another program")
            error_box("AnyKir cannot start: another program on this PC is using port 47653 (127.0.0.1).")
        return
    updater.restart_args = args[:]
    if updater.apply_if_staged():
        trace("switched to a newer saved release")
        return
    if "--nogui" not in args:
        dpi_aware()
        threading.Thread(target=preload, daemon=True).start()
    if "--nogui" not in args and "--minimized" not in args:
        trace("asking for the app password")
        if not unlock(action="open"):
            trace("password box closed without the right password")
            return
    trace("unlocked")
    if "--nogui" not in args:
        import fastcopy
        fastcopy.prepare_in_background(on_ready=_fast_copy_ready)
    if "--viewer" in args:
        mode = "viewer"
    elif any(a in args for a in ("--host", "--minimized", "--nogui")):
        mode = "host"
    else:
        mode = choose()
    args = [a for a in args if a not in ("--host", "--viewer")]
    while mode:
        trace(f"mode {mode}")
        if mode == "host":
            updater.restart_args = args if "--nogui" in args else ["--host", "--minimized"] + [a for a in args if a != "--minimized"]
            import host
            if not host.main(args):
                return
            args = [a for a in args if a != "--minimized"]
            mode = choose()
            continue
        updater.restart_args = ["--viewer"]
        import viewer
        if not viewer.main():
            return
        mode = choose()


if __name__ == "__main__":
    try:
        main()
        trace("exit")
    except Exception:
        details = traceback.format_exc()
        trace("crashed: " + details.strip().splitlines()[-1])
        try:
            with open(os.path.join(config_dir(), "crash.log"), "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S\n") + details + "\n")
        except OSError:
            pass
        error_box("AnyKir could not start:\n\n" + details[-1500:])
        raise
