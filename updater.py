import base64
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from common import CHUNK, T_CHUNK, VERSION, config_dir, load_json, save_json

PUBLIC_KEY = "xcmp8bjQH/65z3xZz031DrEHMfnqRWSWPfjhLr+jIDk="
FEED_PORT = 7002
DEFAULT_SOURCES = ['http://192.168.210.112:7002', 'http://100.123.9.43:7002']
CHECK_EVERY = 300
MAX_RELEASE = 300 * 1024 * 1024
EXE_NAME = "AnyKir.exe"
META = os.path.join("release", "release.json")
APK_NAME = "AnyKir.apk"
ANDROID_META = os.path.join("release", "android.json")

restart_args = []
quit_hook = {"fn": None}


def parse_version(text):
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(text or ""))
    return tuple(int(x) for x in m.groups()) if m else None


def newer(a, b):
    pa, pb = parse_version(a), parse_version(b)
    return pa is not None and pb is not None and pa > pb


def signed_message(version, sha256_hex):
    return f"anykir-release-v1|{version}|{sha256_hex}".encode()


def android_message(version, sha256_hex):
    return f"anykir-android-v1|{version}|{sha256_hex}".encode()


def android_staged():
    meta = load_json(ANDROID_META)
    if parse_version(meta.get("version")) and meta.get("sig") and os.path.exists(os.path.join(release_dir(), APK_NAME)):
        return meta
    return None


def install_android(apk_path, version, sig):
    shutil.copy2(apk_path, os.path.join(release_dir(), APK_NAME))
    save_json(ANDROID_META, {"version": version, "sig": sig})


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def verify(path, version, sig):
    if parse_version(version) is None:
        return False
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(PUBLIC_KEY))
        key.verify(base64.b64decode(sig), signed_message(version, file_sha256(path)))
        return True
    except (InvalidSignature, ValueError, TypeError, OSError):
        return False


def release_dir():
    path = os.path.join(config_dir(), "release")
    os.makedirs(path, exist_ok=True)
    return path


def staged():
    meta = load_json(META)
    if parse_version(meta.get("version")) and meta.get("sig") and os.path.exists(os.path.join(release_dir(), EXE_NAME)):
        return meta
    return None


def install_release(exe_path, version, sig):
    dest = os.path.join(release_dir(), EXE_NAME)
    if os.path.abspath(exe_path) != os.path.abspath(dest):
        shutil.copy2(exe_path, dest)
    save_json(META, {"version": version, "sig": sig})


def stage(part, version, sig):
    if not newer(version, VERSION):
        return "not newer than the running version"
    kept = staged()
    if kept and newer(kept["version"], version):
        return "older than the release already kept"
    if not verify(part, version, sig):
        return "bad signature"
    dest = os.path.join(release_dir(), EXE_NAME)
    os.replace(part, dest)
    save_json(META, {"version": version, "sig": sig})
    return None


class UpdateReceiver:

    def __init__(self):
        self.f = None
        self.path = os.path.join(release_dir(), "incoming.part")
        self.meta = None
        self.size = self.got = 0

    @property
    def active(self):
        return self.f is not None

    def start(self, m):
        self.abort()
        size = int(m.get("size", 0))
        if not 0 < size <= MAX_RELEASE or not isinstance(m.get("sig"), str) or parse_version(m.get("ver")) is None:
            raise ValueError("bad update header")
        if not newer(m["ver"], VERSION):
            raise ValueError("not newer than the running version")
        self.meta, self.size, self.got = {"version": m["ver"], "sig": m["sig"]}, size, 0
        self.f = open(self.path, "wb")

    def chunk(self, body):
        self.got += len(body)
        if self.got > self.size:
            self.abort()
            raise ValueError("more data than announced")
        self.f.write(body)

    def finish(self):
        if not self.f:
            return "no update in progress"
        self.f.close()
        self.f = None
        err = "incomplete download" if self.got != self.size else stage(self.path, **self.meta)
        if err:
            self._remove()
        return err

    def abort(self):
        if self.f:
            try:
                self.f.close()
            except OSError:
                pass
            self.f = None
            self._remove()

    def _remove(self):
        try:
            os.remove(self.path)
        except OSError:
            pass


def send_release(ch, stop=None, progress=None):
    meta = staged()
    if not meta:
        return False
    path = os.path.join(release_dir(), EXE_NAME)
    size, sent = os.path.getsize(path), 0
    ch.send_json({"t": "upd_start", "ver": meta["version"], "sig": meta["sig"], "size": size})
    with open(path, "rb") as f:
        while True:
            if stop is not None and stop.is_set():
                return False
            data = f.read(CHUNK)
            if not data:
                break
            ch.send(T_CHUNK, data)
            sent += len(data)
            if progress:
                progress(sent, size)
    ch.send_json({"t": "upd_end"})
    return True


def _wait_pid(pid, timeout):
    if sys.platform != "win32":
        return
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = ctypes.c_void_p
    k.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k.OpenProcess(0x100000, False, pid)
    if h:
        k.WaitForSingleObject(h, int(timeout * 1000))
        k.CloseHandle(h)


def startup():
    pid = os.environ.pop("ANYKIR_WAIT_PID", "")
    if pid.isdigit():
        _wait_pid(int(pid), 20)
    if getattr(sys, "frozen", False):
        aside = sys.executable + ".old"

        def cleanup():
            for _ in range(20):
                try:
                    if os.path.exists(aside):
                        os.remove(aside)
                    return
                except OSError:
                    time.sleep(0.5)
        threading.Thread(target=cleanup, daemon=True).start()


def apply_if_staged():
    if not getattr(sys, "frozen", False):
        return False
    meta = staged()
    if not meta or not newer(meta["version"], VERSION):
        return False
    new = os.path.join(release_dir(), EXE_NAME)
    if not verify(new, meta["version"], meta["sig"]):
        return False
    import fastcopy
    if fastcopy.is_fast():
        exe = fastcopy.home_exe()
        if not exe:
            return _launch(new)
    else:
        exe = sys.executable
    aside = exe + ".old"
    fallback = (lambda: _launch(new)) if fastcopy.is_fast() else (lambda: False)
    try:
        if os.path.exists(aside):
            os.remove(aside)
        os.replace(exe, aside)
    except OSError:
        return fallback()
    try:
        shutil.copy2(new, exe)
    except OSError:
        try:
            os.replace(aside, exe)
        except OSError:
            pass
        return fallback()
    return _launch(exe)


def _launch(exe):
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1", ANYKIR_WAIT_PID=str(os.getpid()))
    subprocess.Popen([exe] + restart_args, env=env, close_fds=True,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 8) | subprocess.CREATE_NEW_PROCESS_GROUP)
    return True


def restart_now():
    if not apply_if_staged():
        return False
    killer = threading.Timer(4, lambda: os._exit(0))
    killer.daemon = True
    killer.start()
    fn = quit_hook["fn"]
    if fn:
        fn()
    else:
        os._exit(0)
    return True


def user_sources():
    return [x for x in load_json("update.json").get("sources", []) if isinstance(x, str)]


def set_sources(text):
    items = [x.strip() for x in re.split(r"[,;\n]+", text or "") if x.strip()]
    save_json("update.json", {"sources": items})


def sources():
    return list(dict.fromkeys(user_sources() + DEFAULT_SOURCES))


def _is_url(src):
    return bool(re.match(r"https?://", src, re.I)) or (not os.path.isdir(src) and bool(re.fullmatch(r"[\w.-]+(:\d+)?(/.*)?", src)))


def _open_feed(src, name):
    if _is_url(src):
        url = src if re.match(r"https?://", src, re.I) else "http://" + src
        return urllib.request.urlopen(urllib.request.Request(url.rstrip("/") + "/" + name,
                                                             headers={"User-Agent": "AnyKir"}), timeout=15)
    return open(os.path.join(src, name), "rb")


def check_feed():
    for src in sources():
        part = os.path.join(release_dir(), "feed.part")
        try:
            with _open_feed(src, "latest.json") as f:
                meta = json.loads(f.read(65536))
            ver, sig, name = meta["version"], meta["sig"], meta.get("file", EXE_NAME)
            kept = staged()
            if name != os.path.basename(name) or not newer(ver, kept["version"] if kept and newer(kept["version"], VERSION) else VERSION):
                continue
            got = 0
            with _open_feed(src, name) as f, open(part, "wb") as out:
                for block in iter(lambda: f.read(1 << 20), b""):
                    got += len(block)
                    if got > MAX_RELEASE:
                        raise ValueError("release too large")
                    out.write(block)
            if stage(part, ver, sig) is None:
                return ver
        except Exception:
            pass
        try:
            os.remove(part)
        except OSError:
            pass
    return None


def update_waiting():
    kept = staged()
    return bool(kept and newer(kept["version"], VERSION))


class _FeedHandler(BaseHTTPRequestHandler):

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, name, kind):
        path = os.path.join(release_dir(), name)
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(os.path.getsize(path)))
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def do_GET(self):
        meta, phone = staged(), android_staged()
        route = self.path.split("?")[0]
        if phone and route == "/android.json":
            self._json({"version": phone["version"], "sig": phone["sig"], "file": APK_NAME,
                        "size": os.path.getsize(os.path.join(release_dir(), APK_NAME))})
        elif phone and route == "/" + APK_NAME:
            self._file(APK_NAME, "application/vnd.android.package-archive")
        elif meta and route == "/latest.json":
            self._json({"version": meta["version"], "sig": meta["sig"], "file": EXE_NAME})
        elif meta and route == "/" + EXE_NAME:
            self._file(EXE_NAME, "application/octet-stream")
            try:
                save_json("last_update_delivery.json", {"version": meta["version"],
                          "peer": self.client_address[0], "time": time.time(), "source": "feed"})
            except OSError:
                pass
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass


_server = {"srv": None}


class _FeedServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if sys.platform == "win32":
            import socket as _socket
            self.socket.setsockopt(_socket.SOL_SOCKET, getattr(_socket, "SO_EXCLUSIVEADDRUSE", -5), 1)
        super().server_bind()


def ensure_server():
    if _server["srv"] is None and staged():
        try:
            srv = _FeedServer(("0.0.0.0", FEED_PORT), _FeedHandler)
        except OSError:
            return
        _server["srv"] = srv
        threading.Thread(target=srv.serve_forever, daemon=True).start()


class Watcher:

    def __init__(self, on_ready):
        self.on_ready = on_ready

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        time.sleep(20)
        while True:
            try:
                check_feed()
                ensure_server()
                if update_waiting():
                    self.on_ready()
            except Exception:
                pass
            time.sleep(CHECK_EVERY)
