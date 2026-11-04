import base64
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
VERSION = "1.1.9"          # publish.py rewrites this line
DEFAULT_RELAY_PORT = 7000
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


def acquire_single_instance():
    srv = socket.socket()
    try:
        srv.bind(("127.0.0.1", SINGLE_PORT))
    except OSError:
        try:
            with socket.create_connection(("127.0.0.1", SINGLE_PORT), timeout=2) as c:
                c.sendall(b"show")
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
