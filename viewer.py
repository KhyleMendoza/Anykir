__import__("os").environ.setdefault("OPENBLAS_NUM_THREADS", "1")
import ctypes
import io
import json
import math
import os
import queue
import socket
import struct
import sys
import threading
import time

import hub
import updater
from common import (APP, CHUNK, VERSION, acquire_single_instance, show_hook, DEFAULT_DIRECT_PORT, T_CHUNK, T_FRAME, T_JSON,
                    AuthError, ConnectError, client_handshake, derive_key, load_json, parse_addr,
                    protect_secret, read_line, save_json, send_line, tune, unlock, unprotect_secret)

if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

from PIL import Image, ImageTk


class ViewerSession:

    def __init__(self):
        self.ch = None
        self.events = queue.Queue()
        self.fb = None
        self.fb_lock = threading.Lock()
        self.dirty = False
        self.dirty_box = None
        self.view = None
        self.view_size = None
        self.view_box = None
        self.frames = 0
        self.rx = 0
        self.download = None
        self.pending_dl = {}
        self.cancel_upload = threading.Event()
        self.upd = updater.UpdateReceiver()
        self.closed = False
        self.view_only = False

    def connect(self, address, password, sock=None):
        try:
            if sock is None:
                sock = socket.create_connection(parse_addr(address, DEFAULT_DIRECT_PORT), timeout=10)
            tune(sock)
            sock.settimeout(15)
            self.ch = client_handshake(sock, password)
            sock.settimeout(None)
            self.ch.send_json({"t": "caps", "ack": 1})
        except AuthError:
            if sock:
                sock.close()
            raise
        except (OSError, ConnectionError) as e:
            raise ConnectError(f"Could not connect: {e}") from e
        threading.Thread(target=self._recv_loop, daemon=True).start()

    def close(self):
        self.closed = True
        if self.ch:
            self.ch.close()

    CONTROL = frozenset(("mouse", "wheel", "key", "combo", "lock", "cad", "clip", "put", "put_end", "put_done"))

    def send(self, obj):
        if self.view_only and obj.get("t") in self.CONTROL:
            return
        if self.ch and not self.closed:
            try:
                self.ch.send_json(obj)
            except OSError:
                self.close()

    def upload(self, path, remote_dir=""):
        if self.view_only:
            return
        threading.Thread(target=self._upload, args=(path, remote_dir), daemon=True).start()

    def _upload(self, path, remote_dir):
        self.cancel_upload.clear()
        try:
            size = os.path.getsize(path)
            self.send({"t": "put", "name": os.path.basename(path), "dir": remote_dir})
            sent = 0
            with open(path, "rb") as f:
                while not self.closed and not self.cancel_upload.is_set():
                    data = f.read(CHUNK)
                    if not data:
                        break
                    self.ch.send(T_CHUNK, data)
                    sent += len(data)
                    self.events.put(("progress", "Uploading " + os.path.basename(path), sent, size))
            self.send({"t": "put_end"})
        except OSError as e:
            self.events.put(("msg", {"t": "put_done", "ok": False, "err": str(e)}))

    def push_update(self):
        threading.Thread(target=self._push_update, daemon=True).start()

    def _push_update(self):
        try:
            updater.send_release(self.ch, None, lambda done, total: self.events.put(
                ("progress", "Sending update to host", done, total)))
        except OSError as e:
            self.events.put(("msg", {"t": "upd_done", "ok": False, "err": str(e)}))

    def download_file(self, remote_path, dest):
        self.pending_dl = {"dest": dest}
        self.send({"t": "get", "path": remote_path})

    def _view_job(self, im, x, y, w, h):
        view, vs = self.view, self.view_size
        if view is None or vs is None or view.size != vs:
            return None
        fw, fh = self.fb.size
        if vs == (fw, fh):
            view.paste(im, (x, y))
            self._mark_view((x, y, x + w, y + h))
            return None
        sx, sy = vs[0] / fw, vs[1] / fh
        dx0, dy0 = max(int(x * sx) - 1, 0), max(int(y * sy) - 1, 0)
        dx1, dy1 = min(int(math.ceil((x + w) * sx)) + 1, vs[0]), min(int(math.ceil((y + h) * sy)) + 1, vs[1])
        if dx1 <= dx0 or dy1 <= dy0:
            return None
        src = (dx0 / sx, dy0 / sy, dx1 / sx, dy1 / sy)
        cx0, cy0 = max(int(src[0]) - 2, 0), max(int(src[1]) - 2, 0)
        cx1, cy1 = min(int(math.ceil(src[2])) + 2, fw), min(int(math.ceil(src[3])) + 2, fh)
        crop = self.fb.crop((cx0, cy0, cx1, cy1))
        return view, crop, (src[0] - cx0, src[1] - cy0, src[2] - cx0, src[3] - cy0), (dx0, dy0, dx1, dy1)

    def _scale_piece(self, view, crop, src, box):
        part = crop.resize((box[2] - box[0], box[3] - box[1]), Image.BILINEAR, box=src)
        with self.fb_lock:
            if self.view is view:
                view.paste(part, box[:2])
                self._mark_view(box)

    def _mark_view(self, n):
        b = self.view_box
        self.view_box = n if b is None else (min(b[0], n[0]), min(b[1], n[1]), max(b[2], n[2]), max(b[3], n[3]))

    def _recv_loop(self):
        reason = "Connection closed"
        try:
            while True:
                mtype, body = self.ch.recv()
                if mtype == T_FRAME:
                    x, y, w, h = struct.unpack(">HHHH", body[:8])
                    im = Image.open(io.BytesIO(body[8:]))
                    im.load()
                    job = None
                    with self.fb_lock:
                        if self.fb is not None:
                            self.fb.paste(im, (x, y))
                            b, n = self.dirty_box, (x, y, x + w, y + h)
                            self.dirty_box = n if b is None else (min(b[0], n[0]), min(b[1], n[1]),
                                                                  max(b[2], n[2]), max(b[3], n[3]))
                            self.dirty = True
                            self.frames += 1
                            job = self._view_job(im, x, y, w, h)
                    if job:
                        self._scale_piece(*job)
                    self.rx += 1
                    self.send({"t": "ack", "n": self.rx})
                elif mtype == T_CHUNK:
                    self._on_chunk(body)
                elif mtype == T_JSON:
                    m = json.loads(body)
                    t = m.get("t")
                    if t == "screen":
                        with self.fb_lock:
                            self.fb = Image.new("RGB", (m["w"], m["h"]))
                            self.dirty_box = (0, 0, m["w"], m["h"])
                            self.dirty = True
                            self.view = None
                        self.events.put(("msg", m))
                    elif t == "file_start":
                        self._on_file_start(m)
                    elif t == "file_end":
                        self._on_file_end(m)
                    elif t == "upd_start":
                        self._on_upd_start(m)
                    elif t == "upd_end":
                        err = self.upd.finish()
                        self.events.put(("msg", {"t": "upd_failed", "err": err} if err else {"t": "upd_ready"}))
                    else:
                        self.events.put(("msg", m))
        except Exception as e:
            reason = str(e) or type(e).__name__
        self.closed = True
        self.events.put(("closed", reason))

    def _on_file_start(self, m):
        dest = self.pending_dl.get("dest") if self.pending_dl else None
        if not dest:
            return
        try:
            self.download = [open(dest, "wb"), dest, m["size"], 0, m["name"]]
        except OSError as e:
            self.download = None
            self.events.put(("msg", {"t": "get_done", "ok": False, "err": str(e)}))
            self.send({"t": "cancel"})

    def _on_upd_start(self, m):
        try:
            self.upd.start(m)
        except (ValueError, OSError) as e:
            self.events.put(("msg", {"t": "upd_failed", "err": str(e)}))

    def _on_chunk(self, body):
        if self.upd.active:
            try:
                self.upd.chunk(body)
                self.events.put(("progress", "Downloading update", self.upd.got, self.upd.size))
            except ValueError as e:
                self.events.put(("msg", {"t": "upd_failed", "err": str(e)}))
            return
        d = self.download
        if d:
            d[0].write(body)
            d[3] += len(body)
            self.events.put(("progress", "Downloading " + d[4], d[3], d[2]))

    def _on_file_end(self, m):
        d, self.download = self.download, None
        if d:
            d[0].close()
            self.events.put(("msg", {"t": "get_done", "ok": m.get("ok", False), "err": m.get("err"), "path": d[1]}))


def _fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


SHORTCUTS = [
    ("Alt+Tab", ["alt", "tab"]), ("Win key", ["cmd"]), ("Win+D  (desktop)", ["cmd", "d"]),
    ("Ctrl+Alt+Del  (unlock / security)", "cad"),
    ("Win+E  (explorer)", ["cmd", "e"]), ("Win+R  (run)", ["cmd", "r"]), ("Win+L  (lock)", "lock"),
    ("Ctrl+Shift+Esc  (task manager)", ["ctrl", "shift", "esc"]), ("Ctrl+Esc  (start)", ["ctrl", "esc"]),
    ("Alt+F4", ["alt", "f4"]), ("Print Screen", ["print_screen"]),
]


CURSOR_MAP = {"arrow": "arrow", "ibeam": "xterm", "wait": "watch", "busy": "watch", "cross": "crosshair",
              "up": "sb_up_arrow", "size_nwse": "size_nw_se", "size_nesw": "size_ne_sw", "size_we": "size_we",
              "size_ns": "size_ns", "size_all": "fleur", "no": "no", "hand": "hand2", "help": "question_arrow"}
ARROW = (0, 0, 0, 17, 4, 13, 8, 20, 11, 19, 7, 12, 12, 12)
LOCKED_TEXT = ("The host PC is locked (or showing a secure screen). To unlock it from here, install the "
               "admin service on that PC.")
BLOCKED_TEXT = ("That window runs as administrator. Run the host as administrator, or install "
                "AnyKir's administrator-access service to control administrator windows and UAC prompts.")


def _is_tailscale(ip):
    try:
        a, b = (int(x) for x in ip.split(".")[:2])
        return a == 100 and 64 <= b <= 127
    except ValueError:
        return False


class FileBrowser:
    def __init__(self, app):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.app = tk, app
        self.win = tk.Toplevel(app.root)
        self.win.title("Remote files")
        self.win.geometry("720x480")
        top = ttk.Frame(self.win, padding=6)
        top.pack(fill="x")
        ttk.Button(top, text="Up", width=5, command=self.up).pack(side="left")
        self.path_var = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.path_var)
        e.pack(side="left", fill="x", expand=True, padx=6)
        e.bind("<Return>", lambda _e: self.list(self.path_var.get()))
        ttk.Button(top, text="Go", width=5, command=lambda: self.list(self.path_var.get())).pack(side="left")

        cols = ("size", "modified")
        self.tree = ttk.Treeview(self.win, columns=cols, show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Name")
        self.tree.heading("size", text="Size")
        self.tree.heading("modified", text="Modified")
        self.tree.column("#0", width=380)
        self.tree.column("size", width=100, anchor="e")
        self.tree.column("modified", width=150)
        self.tree.pack(fill="both", expand=True, padx=6)
        self.tree.bind("<Double-1>", self._open)

        bot = ttk.Frame(self.win, padding=6)
        bot.pack(fill="x")
        ttk.Button(bot, text="Download selected", command=self.download).pack(side="left")
        ttk.Button(bot, text="Upload here...", command=self.upload).pack(side="left", padx=6)
        ttk.Button(bot, text="Refresh", command=lambda: self.list(self.path_var.get())).pack(side="left")
        self.info = tk.StringVar()
        ttk.Label(bot, textvariable=self.info).pack(side="right")
        self.cur = ""
        self.list("")

    def list(self, path):
        self.app.session.send({"t": "ls", "path": path})

    def up(self):
        if not self.cur:
            return
        parent = os.path.dirname(self.cur.rstrip("\\/"))
        self.list(parent if parent and parent != self.cur else "")

    def show(self, m):
        if m.get("err"):
            self.info.set(m["err"])
            return
        self.cur = m["path"]
        self.path_var.set(self.cur)
        self.tree.delete(*self.tree.get_children())
        for name, is_dir, size, mtime in m["entries"]:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)) if mtime else ""
            self.tree.insert("", "end", text=("[DIR]  " if is_dir else "        ") + name, iid=name,
                             values=("" if is_dir else _fmt_size(size), when), tags=("d" if is_dir else "f",))
        self.info.set(f"{len(m['entries'])} items")

    def _full(self, name):
        return os.path.join(self.cur, name) if self.cur else name

    def _open(self, _e):
        sel = self.tree.selection()
        if sel and "d" in self.tree.item(sel[0], "tags"):
            self.list(self._full(sel[0]))

    def download(self):
        from tkinter import filedialog
        sel = self.tree.selection()
        if not sel or "d" in self.tree.item(sel[0], "tags"):
            self.info.set("Select a file first")
            return
        dest = filedialog.asksaveasfilename(parent=self.win, initialfile=sel[0])
        if dest:
            self.app.session.download_file(self._full(sel[0]), dest)

    def upload(self):
        from tkinter import filedialog
        for p in filedialog.askopenfilenames(parent=self.win):
            self.app.session.upload(p, self.cur)


class ViewerApp:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk, self.root = tk, ttk, root
        root.title(APP + " Viewer")
        self.session = None
        self.files = None
        self.photo = None
        self.photo_size = None
        self.last_move = 0.0
        self.pressed = set()
        self.last_clip = None
        self.remote_screen = None
        self.rcur = None
        self.cur_shape = None
        self.warn = ""
        self.capture_warn = ""
        self.saved = load_json("viewer.json")
        self.connect_frame = None
        self.session_frame = None
        self._filled = False
        self._build_connect()
        show_hook["fn"] = lambda: root.after(0, self._raise)
        updater.quit_hook["fn"] = lambda: root.after(0, root.destroy)
        root.protocol("WM_DELETE_WINDOW", self._quit)
        self.host_updating = False
        self.update_ready = False
        updater.ensure_server()
        updater.Watcher(lambda: root.after(0, self._update_ready)).start()
        root.after(8, self._pump)
        root.after(1000, self._clip_poll)

    def _build_connect(self):
        import ui
        tk, ttk = self.tk, self.ttk
        ui.classic(self.root, False)
        ui.hide(self.root)
        ui.window_size(self.root, 520, 860)
        f = self.connect_frame = ttk.Frame(self.root)
        f.pack(fill="both", expand=True)
        s = self.saved
        self.vars = {
            "pw": tk.StringVar(), "direct": tk.StringVar(value=s.get("direct", "")),
            "usrc": tk.StringVar(value=", ".join(updater.user_sources() or updater.DEFAULT_SOURCES)),
            "server": tk.StringVar(value=s.get("server", ", ".join(hub.VIEWER_SERVERS))),
        }
        ui.appbar(f, VERSION, back=self._main_menu)
        b = ui.scroll_area(f)
        ui.headline(b, "Connect to a PC")
        addr = ui.field(b, "Address", self.vars["direct"], on_enter=self.connect)
        pw = ui.field(b, "Password", self.vars["pw"], show="•", on_enter=self.connect)
        eye = ttk.Button(pw.box, text="Show", style="Text.TButton", takefocus=False)

        def show_pw():
            hidden = pw.cget("show") != ""
            pw.configure(show="" if hidden else "•")
            eye.configure(text="Hide" if hidden else "Show")
        eye.configure(command=show_pw)
        eye.place(relx=1.0, x=-6, rely=0.5, y=4, anchor="e")
        (pw if self.vars["direct"].get() else addr).focus_set()
        self.remember = tk.BooleanVar(value=s.get("remember", True))
        if sys.platform == "win32":
            ttk.Checkbutton(b, text="Remember password on this PC", variable=self.remember).pack(anchor="w",
                                                                                               pady=(6, 0))
        self.view_only = tk.BooleanVar(value=bool(s.get("view_only", False)))
        mode = ttk.Frame(b)
        mode.pack(anchor="w", pady=(10, 0))
        mode_btns = {}

        def show_mode():
            for key, btn in mode_btns.items():
                btn.configure(style="SegOn.TButton" if self.view_only.get() == key else "Seg.TButton")

        def pick_mode(key):
            self.view_only.set(key)
            show_mode()
        for key, text in ((False, "Control"), (True, "View only")):
            mode_btns[key] = ttk.Button(mode, text=text, command=lambda k=key: pick_mode(k))
            mode_btns[key].pack(side="left", padx=(0, 6))
        show_mode()
        self.mode_note = ttk.Label(mode, style="Muted.TLabel")
        self.view_only.trace_add("write", lambda *_: self.mode_note.configure(
            text="Watch only - this PC can't move the mouse or type" if self.view_only.get() else ""))
        self.mode_note.pack(side="left", padx=(4, 0))
        self.view_only.set(self.view_only.get())
        self.btn = ttk.Button(b, text="Connect", style="Filled.TButton", command=self.connect)
        self.btn.pack(fill="x", pady=(12, 4))
        self.msg = tk.StringVar()
        msg = ui.auto_wrap(ttk.Label(b, textvariable=self.msg, style="Error.TLabel"))
        msg.pack(anchor="w")

        def msg_style(*_):
            text = self.msg.get()
            busy = text.startswith(("Connecting", "Connection lost - reconnecting"))
            msg.configure(style="Muted.TLabel" if busy else "Error.TLabel")
            if text and not msg.winfo_ismapped():
                msg.pack(anchor="w", pady=(2, 0), after=self.btn)
            elif not text and msg.winfo_ismapped():
                msg.pack_forget()
        self.msg.trace_add("write", msg_style)
        msg.pack_forget()
        head = ttk.Frame(b)
        head.pack(fill="x", pady=(16, 8))
        self.online_var = tk.StringVar(value="PCs online")
        ttk.Label(head, textvariable=self.online_var, style="Title.TLabel").pack(side="left")
        self.online_note = ui.auto_wrap(ttk.Label(head, text="checking the server...", style="Muted.TLabel"),
                                        reserve=130, minimum=80)
        self.online_note.pack(side="left", padx=(8, 0))
        self.online_box = ttk.Frame(b)
        self.online_box.pack(fill="x")
        self.online_cards = {}
        self.online_empty = ui.auto_wrap(ttk.Label(self.online_box, text="", style="Muted.TLabel"))
        self.adv = ui.expander(b, "Advanced settings")
        self.adv.head.configure(style="Text.TButton")
        self.adv.head.pack_configure(fill="none", anchor="w", pady=(10, 0))
        ui.field(self.adv, "AnyKir Server", self.vars["server"], on_enter=self.connect)
        ui.field(self.adv, "Update source", self.vars["usrc"], on_enter=self.connect)
        ttk.Label(self.adv, text="Appearance", style="Label.TLabel").pack(anchor="w", pady=(10, 6))
        ui.appearance(self.adv).pack(anchor="w")
        self.adv_btn = self.adv.head
        self.vars["direct"].trace_add("write", self._autofill)
        self._autofill()
        self._poll_online()
        ui.reveal(self.root)

    def _addr_key(self):
        direct = self.vars["direct"].get().strip()
        try:
            host, port = parse_addr(direct, DEFAULT_DIRECT_PORT)
            return f"{host.lower()}:{port}"
        except ValueError:
            return direct.lower()

    def _autofill(self, *_):
        pw = unprotect_secret(self.saved.get("pws", {}).get(self._addr_key()))
        if pw is not None:
            self.vars["pw"].set(pw)
            self._filled = True
        elif self._filled:
            self.vars["pw"].set("")
            self._filled = False

    def _remember_pw(self, password):
        pws = self.saved.setdefault("pws", {})
        token = protect_secret(password) if self.remember.get() else None
        if token:
            pws[self._addr_key()] = token
        else:
            pws.pop(self._addr_key(), None)
        self.saved["remember"] = self.remember.get()
        save_json("viewer.json", self.saved)

    def _raise(self):
        self.root.deiconify()
        self.root.lift()
        self.root.attributes('-topmost', True)
        self.root.after(200, lambda: self.root.attributes('-topmost', False))

    def _toggle_adv(self):
        self.adv.toggle()

    def _poll_online(self):
        frame = self.connect_frame
        if frame is None or not frame.winfo_exists():
            return
        server = self.vars["server"].get().strip()
        servers = hub.viewer_servers(server)

        def fetch():
            try:
                used, hosts = hub.fetch_online_any(servers, timeout=2)
                error = ""
            except (OSError, ValueError, ConnectionError):
                used, hosts, error = None, [], "AnyKir Server not reachable (" + ", ".join(servers) + ")"
            self._online_seen = (server, not error, hosts, time.time(), used)
            try:
                self.root.after(0, lambda: self._show_online(frame, hosts, error))
            except RuntimeError:
                pass
        threading.Thread(target=fetch, daemon=True).start()

    def _show_online(self, frame, hosts, error):
        import ui
        if self.connect_frame is not frame or not frame.winfo_exists():
            return
        self.online = hosts
        keep = set()
        for e in hosts:
            keep.add(e["id"])
            sub = f'{e["peer"]}:{e["port"]}'
            tail = next((ip for ip in e.get("ips", []) if _is_tailscale(ip)), None)
            if tail and tail != e["peer"]:
                sub += f"  -  Tailscale {tail}"
            phone = e.get("os") == "Android"
            tag = "Admin + UAC" if e.get("admin") else ("Phone" if phone else "PC")
            card = self.online_cards.get(e["id"])
            if card is None:
                card = ui.ListCard(self.online_box, e["name"], sub, self._pick_online,
                                   on_open=lambda c: (self._pick_online(c), self.connect()),
                                   icon="phone" if phone else "pc", chip_text=tag)
                card.id = e["id"]
                self.online_cards[e["id"]] = card
            else:
                card.set(e["name"], sub, tag)
        for iid in list(self.online_cards):
            if iid not in keep:
                self.online_cards.pop(iid).destroy()
        self.online_var.set(f"PCs online: {len(hosts)}" if not error else "PCs online")
        self.online_note.configure(text=error)
        if hosts:
            self.online_empty.pack_forget()
        else:
            self.online_empty.configure(text="Nothing online yet. PCs and phones appear here when they share."
                                        if not error else "Type the PC's address above to connect directly.")
            self.online_empty.pack(anchor="w", fill="x")
        self.root.after(3000, self._poll_online)

    def _pick_online(self, card=None):
        entry = next((e for e in getattr(self, "online", []) if card is not None and e["id"] == card.id), None)
        if entry:
            self.vars["direct"].set(f'{entry["peer"]}:{entry["port"]}')

    def connect(self, again=None):
        v = {k: x.get().strip() for k, x in self.vars.items()}
        if again:
            v["direct"], v["pw"] = again["address"], again["pw"]
        else:
            self._retry = None
        if not v["pw"] or not v["direct"]:
            self.msg.set("Enter the PC address and password")
            return
        for old in ("id", "relay", "rkey"):
            self.saved.pop(old, None)
        self.saved["direct"] = v["direct"]
        self.saved["server"] = v["server"]
        save_json("viewer.json", self.saved)
        updater.set_sources(v["usrc"])
        self.btn.state(["disabled"])
        if not again:
            self.msg.set("Connecting...")
        pc_id = again.get("id") if again else None
        sess = ViewerSession()

        def work():
            try:
                online, used = None, None
                seen = getattr(self, "_online_seen", None)
                if seen and seen[0] == v["server"] and time.time() - seen[3] < 8:
                    online, used = (seen[2], seen[4]) if seen[1] else (None, None)
                else:
                    try:
                        used, online = hub.fetch_online_any(hub.viewer_servers(v["server"]), timeout=1.5)
                    except (OSError, ValueError, ConnectionError):
                        pass
                pool = online if online is not None else getattr(self, "online", [])
                entry = (next((e for e in pool if e["id"] == pc_id), None) if pc_id
                         else hub.match(pool, v["direct"]))
                sock = None
                if entry:
                    try:
                        sock = hub.connect_via(used, entry["id"])
                    except (OSError, ValueError, ConnectionError) as e:
                        raise ConnectError(f"Could not connect through the server: {e}") from e
                elif pc_id:
                    raise ConnectError("the PC is not back on the server yet")
                sess.connect(v["direct"], v["pw"], sock=sock)
                self.root.after(0, lambda: self._connected(sess, v["pw"], v["direct"], entry))
            except (ConnectError, AuthError) as e:
                text = str(e)
                self.root.after(0, lambda: self._fail(text))
        threading.Thread(target=work, daemon=True).start()

    def _fail(self, text):
        retry = getattr(self, "_retry", None)
        if retry and time.time() < retry["until"] and self.session is None:
            retry["n"] += 1
            self.msg.set(f"Connection lost - reconnecting (try {retry['n']})... {text}")
            self.btn.state(["!disabled"])
            self.root.after(3000, self._try_again)
            return
        if retry:
            self._retry = None
            text = "Could not reconnect: " + text
        self.msg.set(text)
        self.btn.state(["!disabled"])

    def _try_again(self):
        frame = self.connect_frame
        if getattr(self, "_retry", None) and self.session is None and frame is not None and frame.winfo_exists():
            self.connect(again=self._last)

    def _connected(self, sess, password=None, address="", entry=None):
        try:
            self.last_clip = self.root.clipboard_get()
        except self.tk.TclError:
            self.last_clip = ""
        view_only = bool(self.view_only.get())
        sess.view_only = view_only
        if self.saved.get("view_only", False) != view_only:
            self.saved["view_only"] = view_only
            save_json("viewer.json", self.saved)
        retry, last = getattr(self, "_retry", None), getattr(self, "_last", None) or {}
        full = bool(last.get("full", True)) if retry else True
        self.session = sess
        self._retry = None
        self._last = {"address": address, "pw": password, "id": entry["id"] if entry else None}
        if password is not None:
            self._remember_pw(password)
        self.connect_frame.destroy()
        self._build_session()
        if full:
            self.root.attributes("-fullscreen", True)

    def _build_session(self):
        import ui
        tk, ttk = self.tk, self.ttk
        ui.classic(self.root, True)
        f = self.session_frame = ttk.Frame(self.root)
        f.pack(fill="both", expand=True)
        bar = ttk.Frame(f, padding=4)
        bar.pack(fill="x")

        presets = {"Smooth": {"quality": 65, "scale": 1.0, "fps": 60},
                   "Best quality": {"quality": 85, "scale": 1.0, "fps": 30},
                   "Slow connection": {"quality": 40, "scale": 0.6, "fps": 15}}
        ttk.Label(bar, text="Quality").pack(side="left", padx=(6, 2))
        qvar = tk.StringVar(value="Smooth")
        self.qcb = qcb = ttk.Combobox(bar, textvariable=qvar, values=list(presets), width=15, state="readonly")
        qcb.pack(side="left")
        qcb.bind("<<ComboboxSelected>>", lambda _e: (self.session.send({"t": "cfg", **presets[qvar.get()]}),
                                                     self.canvas.focus_set()))
        self.mon_var = tk.StringVar(value="Monitor 1")
        self.mon_cb = ttk.Combobox(bar, textvariable=self.mon_var, values=["Monitor 1"], width=10, state="readonly")
        self.mon_cb.bind("<<ComboboxSelected>>", lambda _e: (self.session.send(
            {"t": "cfg", "monitor": 0 if self.mon_var.get() == "All" else int(self.mon_var.get().split()[1])}),
            self.canvas.focus_set()))

        self.shortcuts_btn = mb = ttk.Menubutton(bar, text="Shortcuts")
        menu = tk.Menu(mb, tearoff=0)
        for label, keys in SHORTCUTS:
            menu.add_command(label=label, command=lambda k=keys: self._shortcut(k))
        mb["menu"] = menu
        mb.pack(side="left", padx=6)
        self.files_btn = ttk.Button(bar, text="Files", command=self.open_files)
        self.files_btn.pack(side="left", padx=6)
        self.full_btn = ttk.Button(bar, text="Fullscreen", command=self.toggle_full)
        self.full_btn.pack(side="left")
        ttk.Button(bar, text="Disconnect", command=self.disconnect).pack(side="right")
        self.mode_lbl = ttk.Label(bar, text="View only")
        self.status = tk.StringVar(value="Connected")
        ttk.Label(bar, textvariable=self.status).pack(side="right", padx=10)

        self.canvas = tk.Canvas(f, bg="#111", highlightthickness=0, cursor="arrow")
        self.canvas.pack(fill="both", expand=True)
        self.img_id = self.canvas.create_image(0, 0, anchor="nw")
        self.cur_item = self.canvas.create_polygon(*ARROW, fill="white", outline="black", state="hidden")
        self.canvas.focus_set()
        c = self.canvas
        c.bind("<Motion>", self._on_move)
        c.bind("<B1-Motion>", self._on_move)
        c.bind("<B2-Motion>", self._on_move)
        c.bind("<B3-Motion>", self._on_move)
        for n, name in ((1, "left"), (2, "middle"), (3, "right")):
            c.bind(f"<ButtonPress-{n}>", lambda e, b=name: self._on_button(e, b, "down"))
            c.bind(f"<ButtonRelease-{n}>", lambda e, b=name: self._on_button(e, b, "up"))
        c.bind("<MouseWheel>", lambda e: self._wheel(0, 1 if e.delta > 0 else -1))
        c.bind("<Shift-MouseWheel>", lambda e: self._wheel(1 if e.delta > 0 else -1, 0))
        c.bind("<Button-4>", lambda e: self._wheel(0, 1))
        c.bind("<Button-5>", lambda e: self._wheel(0, -1))
        c.bind("<KeyPress>", self._on_key_down)
        c.bind("<KeyRelease>", self._on_key_up)
        c.bind("<FocusOut>", lambda _e: self._release_all())
        c.bind("<Enter>", lambda _e: c.focus_set())
        for seq in ("<Tab>", "<Shift-Tab>", "<Alt_L>", "<F10>"):
            c.bind(seq, lambda _e: "break")
        self.root.protocol("WM_DELETE_WINDOW", self.disconnect)
        self.root.geometry("1280x760")
        self.root.minsize(640, 400)
        self.root.title(f"{APP} Viewer")
        self.progress_until = 0
        self.host_uac = False
        self.last_frames, self.last_fps_t = 0, time.time()
        self.session.send({"t": "cfg", **presets["Smooth"]})
        self._show_mode()

    def _show_mode(self):
        if not self.session.view_only:
            return
        for w in (self.shortcuts_btn, self.files_btn):
            w.pack_forget()
        self.mode_lbl.pack(side="right", padx=(6, 4))
        self.root.title(f"{APP} Viewer - view only")

    def _shortcut(self, keys):
        if keys == "lock":
            from tkinter import messagebox
            after = ("Unlock it from here with Keys > Ctrl+Alt+Del and the Windows password." if self.host_uac else
                     "You will not be able to unlock it from here without the admin service on that PC.")
            if messagebox.askyesno(APP, "Lock the host PC?\n\n" + after):
                self.session.send({"t": "lock"})
        elif keys == "cad":
            self.session.send({"t": "cad"})
        else:
            self.session.send({"t": "combo", "keys": keys})

    def toggle_full(self):
        self.root.attributes("-fullscreen", not self.root.attributes("-fullscreen"))

    def disconnect(self):
        self._retry = None
        self._back_to_connect()
        if self.update_ready:
            self.update_ready = False
            updater.restart_now()

    def _main_menu(self):
        self._retry = None
        if self.session:
            self.session.close()
            self.session = None
        self.back_to_menu = True
        self.root.destroy()

    def _quit(self):
        if unlock(self.root, "quit"):
            if self.session:
                self.session.close()
            self.root.destroy()

    def _back_to_connect(self, message=""):
        sess, self.session = self.session, None
        if sess:
            sess.close()
        if getattr(self, "_last", None):
            self._last["full"] = bool(self.root.attributes("-fullscreen"))
        if self.files and self.files.win.winfo_exists():
            self.files.win.destroy()
        self.files = None
        if self.root.attributes("-fullscreen"):
            self.root.attributes("-fullscreen", False)
        if self.session_frame is not None:
            self.session_frame.destroy()
            self.session_frame = None
        self.photo = self.photo_size = self.rcur = self.cur_shape = None
        self.warn = self.capture_warn = ""
        self.pressed.clear()
        self.host_updating = False
        self.root.title(APP + " Viewer")
        self.root.protocol("WM_DELETE_WINDOW", self._quit)
        self._build_connect()
        if message:
            self.msg.set(message)

    def _layout(self):
        sess = self.session
        with sess.fb_lock:
            if sess.fb is None:
                return None
            fw, fh = sess.fb.size
        cw, ch = max(self.canvas.winfo_width(), 1), max(self.canvas.winfo_height(), 1)
        s = min(cw / fw, ch / fh)
        w, h = max(int(fw * s), 1), max(int(fh * s), 1)
        return (cw - w) // 2, (ch - h) // 2, w, h

    def _norm(self, e):
        lay = self._layout()
        if not lay:
            return None
        x, y, w, h = lay
        return min(max((e.x - x) / w, 0.0), 1.0), min(max((e.y - y) / h, 0.0), 1.0)

    def _on_move(self, e):
        now = time.time()
        if now - self.last_move < 0.008:
            return
        p = self._norm(e)
        if p:
            self.last_move = now
            self.session.send({"t": "mouse", "a": "move", "x": p[0], "y": p[1]})

    def _on_button(self, e, button, action):
        p = self._norm(e)
        if p:
            self.canvas.focus_set()
            self.session.send({"t": "mouse", "a": action, "b": button, "x": p[0], "y": p[1]})

    def _wheel(self, dx, dy):
        self.session.send({"t": "wheel", "dx": dx, "dy": dy})

    def _on_key_down(self, e):
        self.pressed.add((e.keysym, e.char))
        self.session.send({"t": "key", "a": "down", "k": e.keysym, "c": e.char})
        return "break"

    def _on_key_up(self, e):
        self.pressed = {p for p in self.pressed if p[0] != e.keysym}
        self.session.send({"t": "key", "a": "up", "k": e.keysym, "c": e.char})
        return "break"

    def _release_all(self):
        for ks, ch in list(self.pressed):
            self.session.send({"t": "key", "a": "up", "k": ks, "c": ch})
        self.pressed.clear()

    def push_clip(self):
        try:
            text = self.root.clipboard_get()
        except self.tk.TclError:
            return
        self.last_clip = text
        self.session.send({"t": "clip", "text": text})

    def _clip_poll(self):
        if self.session and not self.session.closed and getattr(self, "clipboard_allowed", True):
            try:
                text = self.root.clipboard_get()
                if text != self.last_clip and len(text) < 1_000_000:
                    self.last_clip = text
                    self.session.send({"t": "clip", "text": text})
            except self.tk.TclError:
                pass
        self.root.after(1000, self._clip_poll)

    def open_files(self):
        if self.files and self.files.win.winfo_exists():
            self.files.win.lift()
        else:
            self.files = FileBrowser(self)

    def _pump(self):
        try:
            self._pump_inner()
        finally:
            self.root.after(8 if self.session else 100, self._pump)

    def _pump_inner(self):
        sess = self.session
        if not sess:
            return
        while True:
            try:
                ev = sess.events.get_nowait()
            except queue.Empty:
                break
            if ev[0] == "closed":
                updating = self.host_updating
                self._back_to_connect("The host is updating - reconnecting..." if updating
                                      else "Connection lost - reconnecting...")
                if getattr(self, "_last", None):
                    self._retry = {"until": time.time() + (90 if updating else 60), "n": 0}
                    self.root.after(1500 if not updating else 8000, self._try_again)
                else:
                    self.msg.set("Disconnected: " + ev[1])
                return
            if ev[0] == "progress":
                _, label, done, total = ev
                self.status.set(f"{label}  {_fmt_size(done)} / {_fmt_size(total)}")
                self.progress_until = time.time() + 2
            else:
                self._on_msg(ev[1])
        self._draw()
        self._update_cursor()
        now = time.time()
        if now - self.last_fps_t >= 1 and now > self.progress_until:
            fps = (sess.frames - self.last_frames) / (now - self.last_fps_t)
            self.last_frames, self.last_fps_t = sess.frames, now
            self.status.set(self.capture_warn or self.warn or ("Update ready - installs when you disconnect" if self.update_ready
                                          else f"{fps:.0f} updates/s"))

    def _update_cursor(self):
        m, lay = self.rcur, self._layout()
        if not m or not lay:
            return
        shape = CURSOR_MAP.get(m.get("s"), "arrow")
        if shape != self.cur_shape:
            try:
                self.canvas.configure(cursor=shape)
                self.cur_shape = shape
            except self.tk.TclError:
                pass
        c = self.canvas
        x, y, w, h = lay
        px, py = x + m["x"] * w, y + m["y"] * h
        lx, ly = c.winfo_pointerx() - c.winfo_rootx(), c.winfo_pointery() - c.winfo_rooty()
        show = m.get("vis", True) and abs(lx - px) + abs(ly - py) > 14
        if show:
            c.coords(self.cur_item, *[v + (px if i % 2 == 0 else py) for i, v in enumerate(ARROW)])
            c.tag_raise(self.cur_item)
        c.itemconfigure(self.cur_item, state="normal" if show else "hidden")

    def _offer_update(self, hello):
        from tkinter import messagebox
        if not self.session or self.session.closed:
            return
        mine = updater.staged()
        mine_v = mine["version"] if mine else VERSION
        if not updater.newer(mine_v, VERSION):
            mine_v = VERSION
        host_v, host_rel = hello.get("ver"), hello.get("rel")
        if mine and hello.get("upd") and updater.newer(mine_v, host_v):
            if messagebox.askyesno(APP, f"The host PC runs AnyKir {host_v}; you have {mine_v}.\n\n"
                                        "Update the host now? It will restart and you will need to reconnect."):
                self.session.push_update()
        elif host_rel and updater.newer(host_rel, mine_v):
            if messagebox.askyesno(APP, f"The host PC has a newer AnyKir ({host_rel}; you have {mine_v}).\n\n"
                                        "Download it and update this PC now? AnyKir will restart."):
                self.session.send({"t": "upd_get"})

    def _update_ready(self):
        if self.session is None:
            updater.restart_now()
        else:
            self.update_ready = True

    def _warn_same_pc(self):
        from tkinter import messagebox
        messagebox.showwarning(APP, "You connected to this same PC.\n\nRemote control can't work on yourself "
                                    "(the pointer fights with itself). Connect from a different PC.")

    def _on_msg(self, m):
        t = m.get("t")
        if t == "hello":
            self.host_uac = bool(m.get("uac"))
            access = "  (administrator + UAC)" if m.get("uac") else ("  (administrator)" if m.get("admin") else "")
            self.root.title(f"{APP} Viewer - {m.get('host', '')}" + access + ("" if m.get("input") else "  (view only)"))
            self.clipboard_allowed = m.get("clipboard", True)
            self.files_btn.state(["!disabled"] if m.get("files", True) else ["disabled"])
            if m.get("host") and m["host"] == socket.gethostname():
                self.root.after(100, self._warn_same_pc)
            self.root.after(300, lambda: self._offer_update(m))
        elif t == "upd_done":
            if m.get("ok"):
                self.host_updating = True
                self.status.set("Host received the update and is restarting...")
            else:
                self.status.set("Host refused the update: " + str(m.get("err")))
            self.progress_until = time.time() + 6
        elif t == "upd_ready":
            from tkinter import messagebox
            messagebox.showinfo(APP, "Update downloaded and verified. AnyKir will restart now.")
            if not updater.restart_now():
                self.status.set("Update saved; it will install next time AnyKir starts")
        elif t == "upd_failed":
            self.status.set("Update failed: " + str(m.get("err")))
            self.progress_until = time.time() + 6
        elif t == "cur":
            self.rcur = m
        elif t == "notice":
            self.status.set(str(m.get("text", ""))[:300])
            self.progress_until = time.time() + 8
        elif t == "capture_status":
            self.capture_warn = "" if m.get("ok") else "Screen capture recovering: " + str(m.get("err", "desktop changing"))
            self.status.set(self.capture_warn or self.warn or "Connected")
        elif t == "warn":
            self.warn = LOCKED_TEXT if m.get("locked") else (BLOCKED_TEXT if m.get("blocked") else "")
            self.status.set(self.capture_warn or self.warn or "Connected")
        elif t == "screen":
            n = m.get("count", 1)
            if n > 1 and not self.mon_cb.winfo_ismapped():
                self.mon_cb.pack(side="left", padx=6, after=self.qcb)
            self.mon_cb["values"] = ["All"] + [f"Monitor {i}" for i in range(1, n + 1)]
            self.mon_var.set("All" if m.get("mon") == 0 else f"Monitor {m.get('mon')}")
        elif t == "clip":
            self.last_clip = m["text"]
            self.root.clipboard_clear()
            self.root.clipboard_append(m["text"])
        elif t == "ls_result" and self.files and self.files.win.winfo_exists():
            self.files.show(m)
        elif t == "get_done":
            self.status.set("Download complete" if m.get("ok") else f"Download failed: {m.get('err') or 'cancelled'}")
            self.progress_until = time.time() + 4
        elif t == "put_done":
            self.status.set("Upload complete" if m.get("ok") else f"Upload failed: {m.get('err')}")
            self.progress_until = time.time() + 4
            if self.files and self.files.win.winfo_exists():
                self.files.list(self.files.cur)

    def _draw(self):
        sess = self.session
        size = self._layout_size()
        rebuild = self.photo is None or self.photo_size != size or sess.view is None
        if not rebuild and sess.view_box is None:
            return
        lay = self._layout()
        if not lay:
            return
        x, y, w, h = lay
        if rebuild or sess.view_size != (w, h):
            with sess.fb_lock:
                sess.view_size = (w, h)
                view = sess.fb.copy() if sess.fb.size == (w, h) else sess.fb.resize((w, h), Image.BILINEAR)
                sess.view, sess.view_box = view, None
                img = view.copy()
                sess.dirty = False
            if self.photo is None or self.photo.width() != w or self.photo.height() != h:
                self.photo = ImageTk.PhotoImage(img)
            else:
                self.photo.paste(img)
            self.canvas.itemconfigure(self.img_id, image=self.photo)
            self.canvas.coords(self.img_id, x, y)
            self.photo_size = size
            return
        with sess.fb_lock:
            box, sess.view_box, sess.dirty = sess.view_box, None, False
            most = (box[2] - box[0]) * (box[3] - box[1]) > w * h // 2
            part = sess.view.copy() if most else sess.view.crop(box)
        if most:
            self.photo.paste(part)
        else:
            tmp = ImageTk.PhotoImage(part)
            self.root.tk.call(str(self.photo), "copy", str(tmp), "-to", box[0], box[1])

    def _layout_size(self):
        return self.canvas.winfo_width(), self.canvas.winfo_height()


def main(argv=None):
    import tkinter as tk
    root = tk.Tk()
    try:
        import logo
        logo.tk_icon(root)
    except Exception:
        pass
    import ui
    ui.apply(root)
    app = ViewerApp(root)
    root.mainloop()
    return getattr(app, "back_to_menu", False)


if __name__ == "__main__":
    if acquire_single_instance():
        main()
