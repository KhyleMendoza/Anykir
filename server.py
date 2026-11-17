import ctypes
import os
import socket
import subprocess
import sys
import threading
import time

import hub
from common import APP, VERSION, load_json, save_json, unlock

NAME = "AnyKir Server"
LOCK_PORT = 47657
RULE = "AnyKir Server"


def single_instance():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
    except OSError:
        try:
            with socket.create_connection(("127.0.0.1", LOCK_PORT), timeout=2) as c:
                c.sendall(b"show")
        except OSError:
            pass
        return None
    return s


def autostart_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    pyw = sys.executable.replace("python.exe", "pythonw.exe")
    return f'"{pyw}" "{os.path.abspath(__file__)}" --minimized'


def set_autostart(enable):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                        winreg.KEY_SET_VALUE) as k:
        if enable:
            winreg.SetValueEx(k, "AnyKirServer", 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, "AnyKirServer")
            except FileNotFoundError:
                pass


def netsh(*args):
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "netsh.exe")
    return subprocess.run([exe, "advfirewall", "firewall", *args], capture_output=True, text=True,
                          creationflags=0x08000000)


def firewall_ready(port):
    out = netsh("show", "rule", f"name={RULE}").stdout
    return "Enabled:" in out and str(port) in out


def allow_firewall(port):
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
    command = (f'/c netsh advfirewall firewall delete rule name="{RULE}" & '
               f'netsh advfirewall firewall add rule name="{RULE}" dir=in action=allow protocol=TCP '
               f'localport={int(port)} profile=any')
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, command, None, 0) > 32


def local_addresses():
    try:
        from network_info import local_ips
        return local_ips()
    except Exception:
        return []


def service_text(entry):
    if entry.get("os") == "Android":
        return "-"
    svc = entry.get("svc")
    if svc is None:
        return "Unknown"
    if not svc:
        return "Not installed"
    ver = f"v{svc['ver']}" if svc.get("ver") else "old version"
    return ver if svc.get("state") == "running" else f"{ver}, stopped"


class ServerApp:
    def __init__(self, root, lock, minimized):
        import tkinter as tk
        from tkinter import ttk
        import ui
        self.tk, self.ui, self.root, self.lock = tk, ui, root, lock
        self.cfg = load_json("server.json") or {}
        self.port = int(self.cfg.get("port", hub.HUB_PORT))
        self.hub = None
        self.firewall_ok = None
        root.title(NAME)
        ui.window_size(root, 1180, 720, min_width=640, min_height=480)

        self.bar = ui.appbar(root, VERSION, title=NAME, chip_text="Starting", chip_kind="off", server=True)
        page = ttk.Frame(root)
        page.pack(fill="both", expand=True, padx=20, pady=(6, 18))
        self.page = page

        self.side = ttk.Frame(page)
        c1 = ui.card(self.side, padding=(18, 14))
        ttk.Label(c1, text="Server address", style="Card.Label.TLabel").pack(anchor="w")
        self.addr_var = tk.StringVar(value="-")
        self.addr_lbl = ui.auto_wrap(ttk.Label(c1, textvariable=self.addr_var, style="Card.Big.TLabel",
                                               font=(ui.MONO, 14, "bold")))
        self.addr_lbl.pack(anchor="w", fill="x", pady=(2, 0))
        self.addr_more = ui.auto_wrap(ttk.Label(c1, style="Card.Small.TLabel"))
        self.addr_more.pack(anchor="w", fill="x")
        c2 = ui.card(self.side, padding=(18, 14))
        ttk.Label(c2, text="Online now", style="Card.Label.TLabel").pack(anchor="w")
        self.count_var = tk.StringVar(value="0 devices")
        ui.auto_wrap(ttk.Label(c2, textvariable=self.count_var, style="Card.TLabel", font=(ui.FONT, 17))).pack(
            anchor="w", fill="x")
        self.count_more = ui.auto_wrap(ttk.Label(c2, style="Card.Small.TLabel"))
        self.count_more.pack(anchor="w", fill="x")
        c3 = ui.card(self.side, padding=(18, 4, 18, 12))
        self.auto_var = tk.BooleanVar(value=self.cfg.get("autostart", True))
        ui.setting_row(c3, "Start with Windows", "The server starts by itself", self.auto_var,
                       command=self._save_autostart)
        fw = ui.setting_row(c3, "Windows Firewall", "Checking...")
        self.fw_row = fw
        self.fw_btn = ttk.Button(fw.right, text="Allow", style="CardTonal.TButton", command=self._allow_firewall)
        port_row = ttk.Frame(c3, style="Plain.TFrame")
        port_row.pack(fill="x", pady=(10, 0))
        self.port_var = tk.StringVar(value=str(self.port))
        self.run_btn = ttk.Button(port_row, text="Start Server", style="CardFilled.TButton", command=self.toggle)
        self.run_btn.pack(side="right", padx=(10, 0), pady=(8, 0))
        port_entry = ui.field(port_row, "Port", self.port_var, on_card=True, width=7, on_enter=self._restart)
        port_entry.box.pack_configure(side="left", fill="x", expand=True)
        ttk.Label(c3, text="Appearance", style="Card.Label.TLabel").pack(anchor="w", pady=(14, 6))
        ui.appearance(c3, on_card=True).pack(anchor="w")
        self._cards = (c1, c2, c3)

        self.main = ui.card(page, padding=(18, 14), fill="both", expand=True)
        self.main.pack_forget()
        head = ttk.Frame(self.main, style="Plain.TFrame")
        head.pack(fill="x", pady=(0, 10))
        ttk.Label(head, text="Devices", style="Card.Title.TLabel", font=(ui.SEMI, 13)).pack(side="left")
        self.enable_btn = ttk.Button(head, text="Enable", style="CardText.TButton", command=self._enable)
        self.enable_btn.pack(side="right")
        self.disable_btn = ttk.Button(head, text="Disable", style="CardText.TButton", command=self._disable)
        self.disable_btn.pack(side="right")
        self.kick_btn = ttk.Button(head, text="Disconnect", style="CardText.TButton", command=self._kick)
        self.kick_btn.pack(side="right")
        self.search_var = tk.StringVar()
        search = ui.field(head, "Filter", self.search_var, on_card=True, width=18, font=(ui.FONT, 10))
        search.box.pack_configure(side="left", fill="none", padx=(16, 8), pady=(0, 0))
        self.search_var.trace_add("write", lambda *_: self._tick(once=True))

        self.body = ttk.Frame(self.main, style="Plain.TFrame")
        self.body.pack(fill="both", expand=True)

        cols = (("name", "Name", 140), ("peer", "IP address", 125), ("ips", "Other addresses", 130),
                ("ver", "AnyKir", 60), ("svc", "Admin service", 120), ("mode", "Mode", 105),
                ("since", "Online since", 110), ("busy", "Status", 75))
        frame = self.list_frame = ttk.Frame(self.body, style="Plain.TFrame")
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=[c[0] for c in cols], show="headings")
        for key, title, width in cols:
            self.tree.heading(key, text=title, anchor="w")
            self.tree.column(key, width=width, minwidth=60, anchor="w", stretch=key == "ips")
        bar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        hbar = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=bar.set, xscrollcommand=hbar.set)
        bar.pack(side="right", fill="y")
        hbar.pack(side="bottom", fill="x")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._update_buttons())
        ui.divider(self.main, pady=(10, 8))
        self.status_var = tk.StringVar()
        ui.auto_wrap(ttk.Label(self.main, textvariable=self.status_var, style="Card.Muted.TLabel")).pack(
            anchor="w", fill="x")
        ui.auto_wrap(ttk.Label(self.main, style="Card.Small.TLabel",
                               text="Disconnect stops AnyKir on that device until it is opened again. "
                                    "Disabled devices are refused until you enable them.")).pack(anchor="w", fill="x")
        self._wide = None
        page.bind("<Configure>", self._layout)

        self.tray = self._make_tray()
        root.protocol("WM_DELETE_WINDOW", self._close)
        threading.Thread(target=self._lock_loop, daemon=True).start()
        self._apply_autostart()
        self.start()
        self._refresh_addresses()
        self._keep_feed()
        self._tick()
        if minimized and self.tray:
            root.withdraw()
        root.after(1500, self._check_firewall)

    def _layout(self, e):
        wide = e.width >= 900
        if wide == self._wide:
            return
        self._wide = wide
        self.side.pack_forget()
        self.main.pack_forget()
        for c in self._cards:
            c.pack_forget()
            c.grid_forget()
        if wide:
            self.side.pack(side="left", fill="y", padx=(0, 14))
            self.side.configure(width=330)
            for c in self._cards:
                c.pack(fill="x", pady=(0, 12))
            self.side.pack_propagate(False)
        else:
            self.side.pack_propagate(True)
            self.side.pack(side="top", fill="x", pady=(0, 12))
            c1, c2, c3 = self._cards
            c1.grid(row=0, column=0, sticky="new", padx=(0, 6), pady=(0, 12))
            c2.grid(row=1, column=0, sticky="new", padx=(0, 6))
            c3.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=(6, 0))
            for i in (0, 1):
                self.side.columnconfigure(i, weight=1, uniform="cards")
            self.side.rowconfigure(1, weight=1)
        self.main.pack(side="left", fill="both", expand=True)

    def _restart(self):
        if self.hub:
            self.stop()
        self.start()
        self._check_firewall()

    def start(self):
        try:
            self.port = int(self.port_var.get())
        except ValueError:
            self.port = hub.HUB_PORT
        self.hub = hub.Hub(self.port, blocked=self.cfg.get("blocked", {}))
        if self.hub.start():
            self.status_var.set(f"Listening on port {self.port}. PCs and phones connect to this server by themselves.")
            self.run_btn.configure(text="Stop Server", style="CardTonal.TButton")
            self.ui.set_chip(self.bar.status, "Running", "ok")
            self.cfg["port"] = self.port
            save_json("server.json", self.cfg)
        else:
            self.status_var.set(f"Could not start: {self.hub.error}")
            self.hub = None
            self.run_btn.configure(text="Start Server", style="CardFilled.TButton")
            self.ui.set_chip(self.bar.status, "Stopped", "bad")

    def stop(self):
        if self.hub:
            self.hub.stop()
            self.hub = None
        self.run_btn.configure(text="Start Server", style="CardFilled.TButton")
        self.ui.set_chip(self.bar.status, "Stopped", "off")
        self.status_var.set("Server stopped. PCs keep trying to reconnect every few seconds.")

    def toggle(self):
        if self.hub:
            self.stop()
        else:
            self.start()
            self._check_firewall()

    def _tick(self, once=False):
        hosts = self.hub.online() if self.hub else []
        all_hosts = hosts
        query = self.search_var.get().strip().lower()

        def match(*texts):
            return not query or any(query in str(t).lower() for t in texts)
        hosts = [e for e in hosts if match(e["name"], e["peer"], *e["ips"])]
        blocked = self.hub.blocked if self.hub else self.cfg.get("blocked", {})
        seen = set()
        for e in hosts:
            iid = e["id"]
            seen.add(iid)
            values = (e["name"], e["peer"], ", ".join(ip for ip in e["ips"] if ip != e["peer"]), e["ver"],
                      service_text(e),
                      "Phone" if e.get("os") == "Android" else ("Admin service" if e["admin"] else "App"),
                      time.strftime("%b %d, %H:%M", time.localtime(e["since"])),
                      "In use" if self.hub.sessions(iid) else "Online")
            if self.tree.exists(iid):
                self.tree.item(iid, values=values, tags=())
            else:
                self.tree.insert("", "end", iid=iid, values=values)
        for iid, info in list(blocked.items()):
            if iid in seen or not match(info.get("name") or iid, info.get("peer", "")):
                continue
            seen.add(iid)
            tried = info.get("tried")
            values = (info.get("name") or iid, info.get("peer", ""), "", "", "", "",
                      ("tried " + time.strftime("%H:%M:%S", time.localtime(tried))) if tried else "", "Disabled")
            if self.tree.exists(iid):
                self.tree.item(iid, values=values, tags=("disabled",))
            else:
                self.tree.insert("", "end", iid=iid, values=values, tags=("disabled",))
        for iid in self.tree.get_children():
            if iid not in seen:
                self.tree.delete(iid)
        self.tree.tag_configure("disabled", foreground=self.ui.C["outline"])
        n = len(all_hosts)
        busy = sum(1 for e in all_hosts if self.hub.sessions(e["id"])) if self.hub else 0
        phones = sum(1 for e in all_hosts if e.get("os") == "Android")
        self.count_var.set(f"{n} device{'s' if n != 1 else ''}" + (f"  -  {busy} in use" if busy else ""))
        parts = []
        if n:
            parts.append(f"{n - phones} PC{'s' if n - phones != 1 else ''}" + (
                f", {phones} phone{'s' if phones != 1 else ''}" if phones else ""))
        if blocked:
            parts.append(f"{len(blocked)} disabled")
        self.count_more.configure(text="  -  ".join(parts) or "Nothing connected yet")
        self._update_buttons()
        if once:
            return
        if self.tray:
            self.tray.title = f"{NAME} - {n} devices online"
        self.root.after(1000, self._tick)

    def _selected(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _update_buttons(self):
        iid = self._selected()
        blocked = bool(self.hub and iid in self.hub.blocked)
        online = bool(self.hub and iid and any(e["id"] == iid for e in self.hub.online()))
        self.kick_btn.state(["!disabled"] if online else ["disabled"])
        self.disable_btn.state(["!disabled"] if iid and self.hub and not blocked else ["disabled"])
        self.enable_btn.state(["!disabled"] if blocked else ["disabled"])

    def _row(self, iid):
        values = self.tree.item(iid, "values") if self.tree.exists(iid) else ()
        return (values[0] if values else iid), (values[1] if len(values) > 1 else "")

    def _save_blocked(self):
        self.cfg["blocked"] = dict(self.hub.blocked) if self.hub else self.cfg.get("blocked", {})
        save_json("server.json", self.cfg)

    def _kick(self):
        from tkinter import messagebox
        iid = self._selected()
        if not (iid and self.hub):
            return
        name, _ = self._row(iid)
        if not messagebox.askyesno(NAME, f"Disconnect {name} and stop AnyKir on it?\n\nNobody can connect to it "
                                         "until AnyKir is opened there again or the PC restarts.", parent=self.root):
            return
        self.hub.stop_pc(iid)
        self.status_var.set(f"Stopped AnyKir on {name}.")

    def _disable(self):
        from tkinter import messagebox
        iid = self._selected()
        if not (iid and self.hub):
            return
        name, peer = self._row(iid)
        if not messagebox.askyesno(NAME, f"Disable {name}?\n\nIts session ends and it is refused by this server "
                                         "until you enable it again.", parent=self.root):
            return
        self.hub.disable(iid, name, peer)
        self._save_blocked()
        self.status_var.set(f"{name} is disabled.")

    def _enable(self):
        iid = self._selected()
        if iid and self.hub:
            name, _ = self._row(iid)
            self.hub.enable(iid)
            self._save_blocked()
            self.status_var.set(f"{name} is enabled. It reconnects within about 30 seconds.")

    def _keep_feed(self):
        def work():
            try:
                import updater
                updater.ensure_server()
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()
        self.root.after(60000, self._keep_feed)

    def _refresh_addresses(self):
        def probe():
            ips = local_addresses()
            try:
                self.root.after(0, lambda: self._show_addresses(ips))
            except RuntimeError:
                pass
        threading.Thread(target=probe, daemon=True).start()
        self.root.after(30000, self._refresh_addresses)

    def _show_addresses(self, ips):
        shown = [f"{ip}:{self.port}" + (" (Tailscale)" if ip.startswith("100.") else "") for ip in ips]
        self.addr_var.set(shown[0].split()[0] if shown else "-")
        self.addr_more.configure(text="Also: " + ",  ".join(shown[1:]) if len(shown) > 1 else
                                 "Devices find this server by itself on this network")

    def _firewall_state(self):
        def probe():
            ok = firewall_ready(self.port)
            try:
                self.root.after(0, lambda: self._show_firewall(ok))
            except RuntimeError:
                pass
        threading.Thread(target=probe, daemon=True).start()

    def _show_firewall(self, ok):
        self.firewall_ok = ok
        self.fw_row.sub.configure(text=f"Allowed on port {self.port}" if ok else
                                  f"Other devices may not reach port {self.port}")
        if ok:
            self.fw_btn.pack_forget()
        elif not self.fw_btn.winfo_ismapped():
            self.fw_btn.pack(side="right")

    def _check_firewall(self):
        self._firewall_state()
        if not self.hub or self.cfg.get("firewall_asked_port") == self.port:
            return
        port = self.port

        def probe():
            if not firewall_ready(port):
                try:
                    self.root.after(0, lambda: self._ask_firewall(port))
                except RuntimeError:
                    pass
        threading.Thread(target=probe, daemon=True).start()

    def _ask_firewall(self, port):
        from tkinter import messagebox
        if port != self.port or not self.hub or self.cfg.get("firewall_asked_port") == self.port:
            return
        self.cfg["firewall_asked_port"] = self.port
        save_json("server.json", self.cfg)
        if self.root.state() != "withdrawn" and messagebox.askyesno(
                NAME, f"Allow other PCs to reach {NAME} on port {self.port} through Windows Firewall?\n\n"
                      "Windows will ask for administrator permission.", parent=self.root):
            allow_firewall(self.port)
            self.root.after(1500, self._firewall_state)

    def _allow_firewall(self):
        allow_firewall(self.port)
        self.root.after(1500, self._firewall_state)

    def _save_autostart(self):
        self.cfg["autostart"] = self.auto_var.get()
        save_json("server.json", self.cfg)
        self._apply_autostart()

    def _apply_autostart(self):
        try:
            set_autostart(bool(self.cfg.get("autostart", True)))
        except OSError:
            pass

    def _make_tray(self):
        try:
            import pystray
            import logo
            menu = pystray.Menu(pystray.MenuItem("Open", lambda: self.root.after(0, self.show), default=True),
                                pystray.MenuItem("Quit", lambda: self.root.after(0, self._quit)))
            icon = pystray.Icon("AnyKirServer", logo.server_image(64), NAME, menu)
            icon.run_detached()
            return icon
        except Exception:
            return None

    def _lock_loop(self):
        while True:
            try:
                conn, _ = self.lock.accept()
                with conn:
                    conn.settimeout(1)
                    if conn.recv(8) == b"show":
                        self.root.after(0, self.show)
            except OSError:
                return

    def show(self):
        if self.root.state() == "withdrawn" and not unlock(self.root, "open", server=True):
            return
        self.root.deiconify()
        self.root.lift()

    def _close(self):
        if self.tray:
            self.root.withdraw()
        else:
            self._quit()

    def _quit(self):
        if not unlock(self.root, "quit", server=True):
            return
        self.stop()
        if self.tray:
            self.tray.stop()
        self.root.destroy()


def main():
    minimized = "--minimized" in sys.argv
    lock = single_instance()
    if lock is None:
        return
    import cleanup
    cleanup.clean_in_background()
    if not minimized and not unlock(action="open", server=True):
        return
    import tkinter as tk
    root = tk.Tk()
    try:
        import logo
        logo.tk_icon(root, server=True)
    except Exception:
        pass
    import ui
    ui.apply(root)
    ServerApp(root, lock, minimized)
    ui.reveal(root)
    root.mainloop()


if __name__ == "__main__":
    main()
