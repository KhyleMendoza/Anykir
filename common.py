import base64
import ipaddress
import ctypes
import hashlib
import hmac
import json
import os
import socket
import struct
import sys
import threading

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

APP = "AnyKir"
VERSION = "1.51.9"
APP_LOCK_SALT = "1Du++jnk9++xDFJzI/DlNw=="
APP_LOCK_HASH = "jwyrmRxCTO6qLNZy82Pa/1YLNahVli+BzNc0E2+8mOE="
APP_LOCK_ROUNDS = 200_000
DEFAULT_DIRECT_PORT = 7001
MAX_MSG = 64 * 1024 * 1024
CHUNK = 256 * 1024

T_JSON, T_FRAME, T_CHUNK = 1, 2, 3


class AuthError(Exception):
    pass


class ConnectError(Exception):
    pass


_config_override = None


def set_config_dir(path):
    global _config_override
    _config_override = os.path.abspath(path)


def config_dir():
    if _config_override is not None:
        os.makedirs(_config_override, exist_ok=True)
        return _config_override
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    path = os.path.join(base, APP)
    os.makedirs(path, exist_ok=True)
    return path


def load_json(name, default=None):
    try:
        with open(os.path.join(config_dir(), name), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {} if default is None else default


def save_json(name, data):
    path = os.path.join(config_dir(), name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(fn, data):
    buf = ctypes.create_string_buffer(data, len(data))
    src = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def protect_secret(text):
    if sys.platform != "win32":
        return None
    try:
        return base64.b64encode(_dpapi(ctypes.windll.crypt32.CryptProtectData, text.encode())).decode()
    except OSError:
        return None


def unprotect_secret(token):
    if sys.platform != "win32" or not token:
        return None
    try:
        return _dpapi(ctypes.windll.crypt32.CryptUnprotectData, base64.b64decode(token)).decode()
    except (OSError, ValueError):
        return None


def parse_addr(text, default_port):
    text = text.strip()
    if ":" in text:
        host, _, port = text.rpartition(":")
        return host, int(port)
    return text, default_port


def recv_exact(sock, n):
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
        r = sock.recv_into(view[got:], n - got)
        if r == 0:
            raise ConnectionError("connection closed")
        got += r
    return bytes(buf)


def read_line(sock, limit=4096):
    out = bytearray()
    while True:
        b = sock.recv(1)
        if not b:
            raise ConnectionError("connection closed")
        if b == b"\n":
            return bytes(out)
        out += b
        if len(out) > limit:
            raise ConnectionError("line too long")


def send_line(sock, obj):
    sock.sendall(json.dumps(obj, separators=(",", ":")).encode() + b"\n")


def tune(sock):
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for option, value in (("TCP_KEEPIDLE", 4), ("TCP_KEEPINTVL", 2), ("TCP_KEEPCNT", 3)):
        try:
            sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, option), value)
        except (AttributeError, OSError):
            pass
    if sys.platform == "win32":
        try:
            sock.setsockopt(socket.IPPROTO_TCP, 5, 10)
        except OSError:
            pass


def derive_key(password, peer_id):
    return hashlib.scrypt(password.encode(), salt=b"anykir-v1:" + peer_id.encode(),
                          n=2 ** 14, r=8, p=1, dklen=32)


def _mac(key, *parts):
    return hmac.new(key, b"".join(parts), hashlib.sha256).digest()


class SecureChannel:
    def __init__(self, sock, session_key, send_prefix, recv_prefix):
        self.sock = sock
        self._aes = AESGCM(session_key)
        self._sp, self._rp = send_prefix, recv_prefix
        self._sc = self._rc = 0
        self._lock = threading.Lock()

    def send(self, mtype, body=b""):
        with self._lock:
            nonce = self._sp + struct.pack(">Q", self._sc)
            self._sc += 1
            ct = self._aes.encrypt(nonce, bytes([mtype]) + body, None)
            self.sock.sendall(struct.pack(">I", len(ct)) + ct)

    def send_json(self, obj):
        self.send(T_JSON, json.dumps(obj, separators=(",", ":")).encode())

    def recv(self):
        (n,) = struct.unpack(">I", recv_exact(self.sock, 4))
        if n > MAX_MSG:
            raise ConnectionError("message too large")
        ct = recv_exact(self.sock, n)
        nonce = self._rp + struct.pack(">Q", self._rc)
        self._rc += 1
        pt = self._aes.decrypt(nonce, ct, None)
        return pt[0], pt[1:]

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def client_handshake(sock, password):
    nv = os.urandom(16)
    sock.sendall(b"AK1" + nv)
    id_len = recv_exact(sock, 1)[0]
    peer_id = recv_exact(sock, id_len).decode("ascii", "replace")
    data = recv_exact(sock, 16 + 32)
    nh, proof = data[:16], data[16:]
    key = derive_key(password, peer_id)
    if not hmac.compare_digest(proof, _mac(key, b"host", nv, nh)):
        raise AuthError("Wrong password or ID")
    sock.sendall(_mac(key, b"viewer", nh, nv))
    return SecureChannel(sock, _mac(key, b"session", nv, nh), b"V\0\0\0", b"H\0\0\0")


def server_handshake(sock, key, host_id):
    hello = recv_exact(sock, 19)
    if hello[:3] != b"AK1":
        raise AuthError("bad protocol")
    nv, nh = hello[3:], os.urandom(16)
    ident = host_id.encode("ascii")
    sock.sendall(bytes([len(ident)]) + ident + nh + _mac(key, b"host", nv, nh))
    proof = recv_exact(sock, 32)
    if not hmac.compare_digest(proof, _mac(key, b"viewer", nh, nv)):
        raise AuthError("bad password")
    return SecureChannel(sock, _mac(key, b"session", nv, nh), b"H\0\0\0", b"V\0\0\0")


SINGLE_PORT = 47653
show_hook = {"fn": None}
single_instance = {"answered": True}


def acquire_single_instance():
    srv = socket.socket()
    try:
        srv.bind(("127.0.0.1", SINGLE_PORT))
    except OSError:
        single_instance["answered"] = False
        try:
            with socket.create_connection(("127.0.0.1", SINGLE_PORT), timeout=2) as c:
                c.sendall(b"show")
                single_instance["answered"] = True
        except OSError:
            pass
        return False
    srv.listen(5)

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
                with conn:
                    conn.settimeout(1)
                    if conn.recv(8) == b"show" and show_hook["fn"]:
                        show_hook["fn"]()
            except OSError:
                pass
    threading.Thread(target=loop, daemon=True).start()
    globals()["_single_srv"] = srv
    return True


def check_app_password(text):
    salt, want = base64.b64decode(APP_LOCK_SALT), base64.b64decode(APP_LOCK_HASH)
    got = hashlib.pbkdf2_hmac("sha256", (text or "").encode("utf-8"), salt, APP_LOCK_ROUNDS)
    return hmac.compare_digest(got, want)


_unlock_box = {"win": None, "entry": None}


def unlock(parent=None, action="open", server=False):
    import tkinter as tk
    from tkinter import ttk
    box = _unlock_box["win"]
    if box is not None:
        try:
            if box.winfo_exists():
                box.deiconify()
                box.lift()
                _unlock_box["entry"].focus_force()
                return False
        except tk.TclError:
            pass
    own = parent is None
    if own:
        parent = tk.Tk()
        parent.withdraw()
        try:
            import logo
            logo.tk_icon(parent, server)
        except Exception:
            pass
    import ui
    if own or parent not in ui._roots:
        ui.apply(parent)
        if not own:
            ui.reveal(parent)
    win = tk.Toplevel(parent)
    _unlock_box["win"] = win
    win.title(APP)
    win.resizable(False, False)
    win.attributes("-topmost", True)
    ui.themed_window(win)
    result = {"ok": False, "tries": 0}
    var, msg = tk.StringVar(), tk.StringVar()
    f = ttk.Frame(win, padding=(22, 18, 22, 18))
    f.pack(fill="both", expand=True)
    top = ttk.Frame(f)
    top.pack(fill="x", pady=(0, 12))
    photo = ui.logo_image(top, 36, server)
    if photo:
        ttk.Label(top, image=photo).pack(side="left", padx=(0, 12))
    texts = ttk.Frame(top)
    texts.pack(side="left", fill="x")
    ttk.Label(texts, text=f"{APP} password", style="Title.TLabel").pack(anchor="w")
    ttk.Label(texts, text=f"Enter it to {action}", style="Muted.TLabel").pack(anchor="w")
    entry = ui.field(f, "Password", var, show="•", width=32)
    _unlock_box["entry"] = entry
    ttk.Label(f, textvariable=msg, style="Error.TLabel").pack(anchor="w", pady=(2, 0))
    row = ttk.Frame(f)
    row.pack(fill="x", pady=(8, 0))

    def ok(*_):
        if check_app_password(var.get()):
            result["ok"] = True
            win.destroy()
            return
        result["tries"] += 1
        var.set("")
        if result["tries"] >= 3:
            win.destroy()
        else:
            msg.set("Wrong password")

    ttk.Button(row, text="Unlock", style="Filled.TButton", command=ok).pack(side="right")
    ttk.Button(row, text="Cancel", style="Text.TButton", command=win.destroy).pack(side="right", padx=(0, 6))
    win.bind("<Return>", ok)
    win.bind("<Escape>", lambda _e: win.destroy())
    win.update_idletasks()
    w, h = win.winfo_reqwidth(), win.winfo_reqheight()
    win.geometry(f"+{(win.winfo_screenwidth() - w) // 2}+{(win.winfo_screenheight() - h) // 3}")
    win.deiconify()
    win.lift()
    ui.reveal(win)
    entry.focus_force()
    win.grab_set()
    try:
        win.wait_window()
    finally:
        _unlock_box["win"] = _unlock_box["entry"] = None
    if own:
        parent.destroy()
    return result["ok"]


def clean_allow_list(value):
    items = value.replace(";", ",").replace("\n", ",").split(",") if isinstance(value, str) else list(value or [])
    good, bad = [], []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        try:
            net = ipaddress.ip_network(text, strict=False)
        except ValueError:
            bad.append(text)
            continue
        entry = str(net.network_address) if net.num_addresses == 1 else str(net)
        if entry not in good:
            good.append(entry)
    return good, bad


def ip_allowed(cfg, ip):
    if cfg.get("allow_from_mode") != "list":
        return True
    try:
        addr = ipaddress.ip_address(str(ip or "").split("%")[0])
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    for item in cfg.get("allow_from") or []:
        try:
            if addr in ipaddress.ip_network(item, strict=False):
                return True
        except ValueError:
            pass
    return False
