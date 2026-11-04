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

import updater
from common import (APP, CHUNK, VERSION, acquire_single_instance, show_hook, DEFAULT_DIRECT_PORT, DEFAULT_RELAY_PORT, T_CHUNK, T_FRAME, T_JSON,
                    AuthError, ConnectError, client_handshake, derive_key, load_json, parse_addr,
                    protect_secret, read_line, save_json, send_line, tune, unprotect_secret)

if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

from PIL import Image, ImageTk  # noqa: E402


class ViewerSession:

    def __init__(self):
        self.ch = None
        self.events = queue.Queue()
        self.fb = None
        self.fb_lock = threading.Lock()
        self.dirty = False
        self.dirty_box = None
        self.frames = 0
        self.rx = 0
        self.download = None
        self.pending_dl = {}
        self.cancel_upload = threading.Event()
        self.upd = updater.UpdateReceiver()
        self.closed = False

    def connect(self, peer_id, password, relay="", relay_key="", direct=""):
        peer_id = peer_id.replace(" ", "")
        try:
            if direct:
                sock = socket.create_connection(parse_addr(direct, DEFAULT_DIRECT_PORT), timeout=10)
            else:
                if not relay:
                    raise ConnectError("Enter a relay server or a direct address")
                sock = socket.create_connection(parse_addr(relay, DEFAULT_RELAY_PORT), timeout=10)
                tune(sock)
                send_line(sock, {"op": "connect", "id": peer_id, "key": relay_key})
                reply = json.loads(read_line(sock))
                if not reply.get("ok"):
                    sock.close()
                    raise ConnectError(reply.get("err", "relay refused"))
            tune(sock)
            sock.settimeout(15)
            self.ch = client_handshake(sock, password)
            sock.settimeout(None)
            self.ch.send_json({"t": "caps", "ack": 1})
        except AuthError:
            sock.close()
            raise
        except (OSError, ConnectionError) as e:
            raise ConnectError(f"Could not connect: {e}") from e
        threading.Thread(target=self._recv_loop, daemon=True).start()

    def close(self):
        self.closed = True
        if self.ch:
            self.ch.close()

    def send(self, obj):
        if self.ch and not self.closed:
            try:
                self.ch.send_json(obj)
            except OSError:
                self.close()

    def upload(self, path, remote_dir=""):
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

    def _recv_loop(self):
        reason = "Connection closed"
        try:
            while True:
                mtype, body = self.ch.recv()
                if mtype == T_FRAME:
                    x, y, w, h = struct.unpack(">HHHH", body[:8])
                    im = Image.open(io.BytesIO(body[8:]))
                    im.load()
                    with self.fb_lock:
                        if self.fb is not None:
                            self.fb.paste(im, (x, y))
                            b, n = self.dirty_box, (x, y, x + w, y + h)
                            self.dirty_box = n if b is None else (min(b[0], n[0]), min(b[1], n[1]),
                                                                  max(b[2], n[2]), max(b[3], n[3]))
                            self.dirty = True
                            self.frames += 1
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
    ("Win+E  (explorer)", ["cmd", "e"]), ("Win+R  (run)", ["cmd", "r"]), ("Win+L  (lock)", "lock"),
    ("Ctrl+Shift+Esc  (task manager)", ["ctrl", "shift", "esc"]), ("Ctrl+Esc  (start)", ["ctrl", "esc"]),
    ("Alt+F4", ["alt", "f4"]), ("Print Screen", ["print_screen"]),
]


CURSOR_MAP = {"arrow": "arrow", "ibeam": "xterm", "wait": "watch", "busy": "watch", "cross": "crosshair",
              "up": "sb_up_arrow", "size_nwse": "size_nw_se", "size_nesw": "size_ne_sw", "size_we": "size_we",
              "size_ns": "size_ns", "size_all": "fleur", "no": "no", "hand": "hand2", "help": "question_arrow"}
ARROW = (0, 0, 0, 17, 4, 13, 8, 20, 11, 19, 7, 12, 12, 12)
LOCKED_TEXT = "The host PC is locked (or showing a secure screen). Unlock it at that PC to continue."
BLOCKED_TEXT = ("That window runs as administrator. Run the host as administrator, or install "
                "AnyKir's administrator-access service to control administrator windows and UAC prompts.")


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
        self.host_updating = False
        self.update_ready = False
        updater.ensure_server()
        updater.Watcher(lambda: root.after(0, self._update_ready)).start()
        root.after(8, self._pump)
        root.after(1000, self._clip_poll)

    def _build_connect(self):
        tk, ttk = self.tk, self.ttk
        self.root.geometry("")
        self.root.minsize(1, 1)
        f = self.connect_frame = ttk.Frame(self.root, padding=20)
        f.pack()
        s = self.saved
        self.vars = {
            "id": tk.StringVar(value=s.get("id", "")), "pw": tk.StringVar(),
            "relay": tk.StringVar(value=s.get("relay", "")), "rkey": tk.StringVar(value=s.get("rkey", "")),
            "direct": tk.StringVar(value=s.get("direct", "")),
            "usrc": tk.StringVar(value=", ".join(updater.user_sources())),
        }
        def row(parent, r, label, key, show=""):
            ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", pady=5, padx=(0, 12))
            e = ttk.Entry(parent, textvariable=self.vars[key], width=30, show=show)
            e.grid(row=r, column=1, pady=5)
            e.bind("<Return>", lambda _e: self.connect())
            return e
        ttk.Label(f, text="Connect to a PC", font=("Segoe UI", 13, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        addr = row(f, 1, "Address", "direct")
        pw = row(f, 2, "Password", "pw", "*")
        (pw if self.vars["direct"].get() else addr).focus_set()
        self.remember = tk.BooleanVar(value=s.get("remember", True))
        if sys.platform == "win32":
            ttk.Checkbutton(f, text="Remember password on this PC", variable=self.remember).grid(
                row=3, column=0, columnspan=2, sticky="w")
        self.adv = ttk.Frame(f)
        row(self.adv, 0, "Remote ID", "id")
        row(self.adv, 1, "Relay server", "relay")
        row(self.adv, 2, "Relay key", "rkey", "*")
        row(self.adv, 3, "Update source", "usrc")
        self.adv_btn = ttk.Button(f, text="Advanced (relay)", command=self._toggle_adv)
        self.adv_btn.grid(row=5, column=0, sticky="w", pady=10)
        self.btn = ttk.Button(f, text="Connect", command=self.connect)
        self.btn.grid(row=5, column=1, sticky="e", pady=10)
        self.msg = tk.StringVar()
        ttk.Label(f, textvariable=self.msg, foreground="#b00020", wraplength=320).grid(
            row=6, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text=f"{APP} v{VERSION}", foreground="#777").grid(row=7, column=1, sticky="e")
        for key in ("direct", "id", "relay"):
            self.vars[key].trace_add("write", self._autofill)
        self._autofill()

    def _addr_key(self):
        direct = self.vars["direct"].get().strip()
        if direct:
            try:
                host, port = parse_addr(direct, DEFAULT_DIRECT_PORT)
                return f"{host.lower()}:{port}"
            except ValueError:
                return direct.lower()
        return "{}@{}".format(self.vars["id"].get().replace(" ", ""), self.vars["relay"].get().strip().lower())

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
        if self.adv.winfo_ismapped():
            self.adv.grid_forget()
        else:
            self.adv.grid(row=4, column=0, columnspan=2, sticky="w")

    def connect(self):
        v = {k: x.get().strip() for k, x in self.vars.items()}
        if not v["pw"] or not (v["direct"] or (v["id"] and v["relay"])):
            self.msg.set("Enter the PC address and password")
            return
        self.saved.update({"id": v["id"], "relay": v["relay"], "rkey": v["rkey"], "direct": v["direct"]})
        save_json("viewer.json", self.saved)
        updater.set_sources(v["usrc"])
        self.btn.state(["disabled"])
        self.msg.set("Connecting...")
        sess = ViewerSession()

        def work():
            try:
                sess.connect(v["id"], v["pw"], v["relay"], v["rkey"], v["direct"])
                self.root.after(0, lambda: self._connected(sess, v["pw"]))
            except (ConnectError, AuthError) as e:
                self.root.after(0, lambda: self._fail(str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _fail(self, text):
        self.msg.set(text)
        self.btn.state(["!disabled"])

    def _connected(self, sess, password=None):
        self.session = sess
        if password is not None:
            self._remember_pw(password)
        self.connect_frame.destroy()
        self._build_session()

    def _build_session(self):
        tk, ttk = self.tk, self.ttk
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

        mb = ttk.Menubutton(bar, text="Shortcuts")
        menu = tk.Menu(mb, tearoff=0)
        for label, keys in SHORTCUTS:
            menu.add_command(label=label, command=lambda k=keys: self._shortcut(k))
        mb["menu"] = menu
        mb.pack(side="left", padx=6)
        self.files_btn = ttk.Button(bar, text="Files", command=self.open_files)
        self.files_btn.pack(side="left", padx=6)
        ttk.Button(bar, text="Fullscreen", command=self.toggle_full).pack(side="left")
        ttk.Button(bar, text="Disconnect", command=self.disconnect).pack(side="right")
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
        self.last_frames, self.last_fps_t = 0, time.time()
        self.session.send({"t": "cfg", **presets["Smooth"]})

    def _shortcut(self, keys):
        if keys == "lock":
            from tkinter import messagebox
            if messagebox.askyesno(APP, "Lock the host PC?\n\nYou will not be able to unlock it from here: "
                                        "someone has to sign in at that PC."):
                self.session.send({"t": "lock"})
        else:
            self.session.send({"t": "combo", "keys": keys})

    def toggle_full(self):
        self.root.attributes("-fullscreen", not self.root.attributes("-fullscreen"))

    def disconnect(self):
        if self.session:
            self.session.close()
        self.root.destroy()

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
            self.root.after(8, self._pump)

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
                self.status.set("Disconnected: " + ev[1])
                from tkinter import messagebox
                messagebox.showinfo(APP, "The host updated and is restarting.\nReconnect in about 10 seconds."
                                    if self.host_updating else "Disconnected: " + ev[1])
                self.root.destroy()
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
        full = self.photo is None or self.photo_size != size
        if not sess.dirty and not full:
            return
        lay = self._layout()
        if not lay:
            return
        x, y, w, h = lay
        with sess.fb_lock:
            box, sess.dirty_box, sess.dirty = sess.dirty_box, None, False
            fw, fh = sess.fb.size
            if full or box is None or self.photo.width() != w or self.photo.height() != h:
                full, snap = True, sess.fb.copy()
            else:
                sx, sy = w / fw, h / fh
                dx0, dy0 = max(int(box[0] * sx) - 1, 0), max(int(box[1] * sy) - 1, 0)
                dx1, dy1 = min(int(math.ceil(box[2] * sx)) + 1, w), min(int(math.ceil(box[3] * sy)) + 1, h)
                part = sess.fb.resize((dx1 - dx0, dy1 - dy0), Image.BILINEAR,
                                      box=(dx0 / sx, dy0 / sy, dx1 / sx, dy1 / sy))
        if full:
            img = snap if snap.size == (w, h) else snap.resize((w, h), Image.BILINEAR)
            if self.photo is None or self.photo.width() != w or self.photo.height() != h:
                self.photo = ImageTk.PhotoImage(img)
            else:
                self.photo.paste(img)
            self.canvas.itemconfigure(self.img_id, image=self.photo)
            self.canvas.coords(self.img_id, x, y)
            self.photo_size = size
        else:
            tmp = ImageTk.PhotoImage(part)
            self.root.tk.call(str(self.photo), "copy", str(tmp), "-to", dx0, dy0)

    def _layout_size(self):
        return self.canvas.winfo_width(), self.canvas.winfo_height()


def main(argv=None):
    import tkinter as tk
    root = tk.Tk()
    ViewerApp(root)
    root.mainloop()


if __name__ == "__main__":
    if acquire_single_instance():
        main()
