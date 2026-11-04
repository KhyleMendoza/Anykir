import os
import sys
import time
import traceback

import updater
from common import APP, acquire_single_instance, config_dir, show_hook


def choose():
    import tkinter as tk
    from tkinter import ttk
    root = tk.Tk()
    root.title(APP)
    root.resizable(False, False)
    picked = []
    show_hook["fn"] = lambda: root.after(0, lambda: (root.deiconify(), root.lift()))

    def pick(mode):
        picked.append(mode)
        root.destroy()

    f = ttk.Frame(root, padding=20)
    f.pack()
    ttk.Label(f, text="What do you want to do?", font=("Segoe UI", 13, "bold")).pack(pady=(0, 12))
    for mode, title, sub in (("host", "Share this PC", "Run this on the PC you want to control"),
                             ("viewer", "Connect to a PC", "Run this on the PC you sit at")):
        b = ttk.Button(f, text=f"{title}\n{sub}", command=lambda m=mode: pick(m), width=38)
        b.pack(pady=5, ipady=8)
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
        service_setup.service_update(args)
        return
    if any(a in args for a in ("--install-service", "--uninstall-service", "--free-port")):
        import service_setup
        service_setup.main(args)
        return
    if "--relay-server" in args:
        import asyncio
        import relay
        sys.argv = [sys.argv[0]] + [a for a in args if a != "--relay-server"]
        asyncio.run(relay.main())
        return
    updater.startup()
    if "--nogui" not in args and not acquire_single_instance():
        return
    updater.restart_args = args[:]
    if updater.apply_if_staged():
        return
    if "--viewer" in args:
        mode = "viewer"
    elif any(a in args for a in ("--host", "--minimized", "--nogui")):
        mode = "host"
    else:
        mode = choose()
    args = [a for a in args if a not in ("--host", "--viewer")]
    if mode == "host":
        updater.restart_args = args if "--nogui" in args else ["--host", "--minimized"] + [a for a in args if a != "--minimized"]
    elif mode == "viewer":
        updater.restart_args = ["--viewer"]
    if mode == "host":
        import host
        host.main(args)
    elif mode == "viewer":
        import viewer
        viewer.main()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        with open(os.path.join(config_dir(), "crash.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S\n") + traceback.format_exc() + "\n")
        raise
