import json
import re
import secrets
import socket
import sys
import threading
import time

from common import parse_addr, read_line, send_line, tune

HUB_PORT = 7000
DEFAULT_HUBS = ["192.168.210.112"]
VIEWER_SERVERS = ["192.168.210.112", "100.123.9.43"]
PING_EVERY = 20
RETRY_EVERY = 5
REFUSED_WAIT = 30
MAX_HOSTS = 500


def clean_hubs(value):
    items = value if isinstance(value, list) else re.split(r"[,;\n]+", str(value or ""))
    out = []
    for item in items:
        item = str(item).strip()
        if item and len(item) <= 255 and re.fullmatch(r"[\w.\-]+(:\d{1,5})?", item) and item not in out:
            out.append(item)
    return out[:5]


class Hub:

    def __init__(self, port=HUB_PORT, blocked=None):
        self.port = port
        self.hosts = {}
        self.pending = {}
        self.pipes = {}
        self.blocked = dict(blocked or {})
        self.lock = threading.Lock()
        self.srv = None
        self.error = ""

    def start(self):
        srv = socket.socket()
        try:
            if sys.platform == "win32":
                srv.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", -5), 1)
            srv.bind(("0.0.0.0", self.port))
            srv.listen(64)
        except OSError as e:
            srv.close()
            self.error = f"port {self.port} is in use ({e.strerror or e})"
            return False
        self.srv = srv
        threading.Thread(target=self._accept, daemon=True).start()
        threading.Thread(target=self._pinger, daemon=True).start()
        return True

    def stop(self):
        srv, self.srv = self.srv, None
        if srv:
            srv.close()
        with self.lock:
            entries = list(self.hosts.values())
            self.hosts.clear()
        for e in entries:
            e["sock"].close()

    def _accept(self):
        while self.srv:
            try:
                s, addr = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(s, addr[0]), daemon=True).start()

    def _handle(self, s, peer):
        try:
            s.settimeout(10)
            tune(s)
            msg = json.loads(read_line(s))
        except (OSError, ValueError, ConnectionError):
            s.close()
            return
        if msg.get("op") == "session":
            with self.lock:
                waiter = self.pending.pop(str(msg.get("token", ""))[:64], None)
            if waiter is None:
                s.close()
                return
            s.settimeout(None)
            waiter["sock"] = s
            waiter["event"].set()
            return
        if msg.get("op") == "where":
            try:
                send_line(s, {"ok": True, "ips": self.addresses()})
            except OSError:
                pass
            s.close()
            return
        if msg.get("op") == "list":
            try:
                send_line(s, {"ok": True, "hosts": [self.public(e) for e in self.online()]})
            except OSError:
                pass
            s.close()
            return
        if msg.get("op") == "connect":
            self._bridge(s, str(msg.get("id", ""))[:16], peer)
            return
        hid = str(msg.get("id", ""))
        if msg.get("op") != "hello" or not re.fullmatch(r"\d{6,12}", hid):
            s.close()
            return
        port = msg.get("port")
        if hid in self.blocked:
            self.blocked[hid].update(name=str(msg.get("name", ""))[:64] or peer, peer=peer, tried=time.time())
            try:
                send_line(s, {"op": "refused"})
            except OSError:
                pass
            s.close()
            return
        entry = {"id": hid, "name": str(msg.get("name", ""))[:64] or peer, "peer": peer,
                 "ips": [str(x)[:45] for x in msg.get("ips", []) if isinstance(x, str)][:8],
                 "port": port if isinstance(port, int) and 0 < port < 65536 else 7001,
                 "ver": str(msg.get("ver", ""))[:16], "admin": bool(msg.get("admin")),
                 "os": "Android" if msg.get("os") == "Android" else "Windows",
                 "svc": _service_field(msg.get("svc")),
                 "sock": s, "send": threading.Lock(), "since": time.time()}
        with self.lock:
            if hid not in self.hosts and len(self.hosts) >= MAX_HOSTS:
                s.close()
                return
            old, self.hosts[hid] = self.hosts.get(hid), entry
        if old:
            old["sock"].close()
        s.settimeout(PING_EVERY * 3)
        self._ping(entry)
        try:
            while True:
                line = read_line(s)
                if b'"pong"' in line and entry.get("ping_at"):
                    entry["rtt"] = round((time.perf_counter() - entry.pop("ping_at")) * 1000, 1)
        except (OSError, ConnectionError):
            pass
        finally:
            with self.lock:
                if self.hosts.get(hid) is entry:
                    del self.hosts[hid]
            s.close()

    def _pinger(self):
        while self.srv:
            time.sleep(PING_EVERY)
            with self.lock:
                entries = list(self.hosts.values())
            for e in entries:
                self._ping(e)

    def _ping(self, e):
        try:
            with e["send"]:
                e["ping_at"] = time.perf_counter()
                send_line(e["sock"], {"op": "ping"})
        except OSError:
            e["sock"].close()

    _addresses = ([], 0.0)

    def addresses(self):
        ips, at = self._addresses
        if time.time() - at > 60:
            try:
                from network_info import local_ips
                ips = local_ips()
            except Exception:
                ips = []
            self._addresses = (ips, time.time())
        return ips

    def online(self):
        with self.lock:
            entries = [dict(e) for e in self.hosts.values()]
        return sorted(entries, key=lambda e: e["name"].lower())

    def public(self, e):
        return {k: e[k] for k in ("id", "name", "peer", "ips", "port", "ver", "admin", "os", "svc", "since")} | {
            "sessions": self.sessions(e["id"]), "rtt": e.get("rtt")}

    def sessions(self, hid):
        with self.lock:
            return len(self.pipes.get(hid, ()))

    def kick(self, hid):
        with self.lock:
            entry = self.hosts.pop(hid, None)
            pairs = list(self.pipes.get(hid, ()))
        for pair in pairs:
            for sock in pair:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        if entry:
            entry["sock"].close()

    def stop_pc(self, hid):
        with self.lock:
            entry = self.hosts.get(hid)
        if entry:
            try:
                with entry["send"]:
                    send_line(entry["sock"], {"op": "stop"})
            except OSError:
                pass
        self.kick(hid)

    def disable(self, hid, name="", peer=""):
        self.blocked[hid] = {"name": name, "peer": peer, "tried": 0}
        self.kick(hid)

    def enable(self, hid):
        self.blocked.pop(hid, None)

    def _bridge(self, viewer, hid, viewer_ip=""):
        with self.lock:
            entry = self.hosts.get(hid)
        try:
            if entry is None:
                send_line(viewer, {"ok": False, "err": "That PC is not connected to the server."})
                viewer.close()
                return
            host = self.open_session(entry, viewer_ip)
            send_line(viewer, {"ok": True})
        except (OSError, ConnectionError) as e:
            try:
                send_line(viewer, {"ok": False, "err": str(e)})
            except OSError:
                pass
            viewer.close()
            return
        viewer.settimeout(None)
        tune(host)
        pair = (viewer, host)
        with self.lock:
            self.pipes.setdefault(hid, set()).add(pair)

        def pipe(src, dst):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                for sock in (src, dst):
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        t = threading.Thread(target=pipe, args=(host, viewer), daemon=True)
        t.start()
        pipe(viewer, host)
        t.join(5)
        viewer.close()
        host.close()
        with self.lock:
            self.pipes.get(hid, set()).discard(pair)

    def open_session(self, entry, viewer_ip="", timeout=10):
        token = secrets.token_hex(16)
        waiter = {"event": threading.Event(), "sock": None}
        with self.lock:
            self.pending[token] = waiter
        try:
            with entry["send"]:
                send_line(entry["sock"], {"op": "open", "token": token, "from": viewer_ip})
            if not waiter["event"].wait(timeout):
                raise ConnectionError("the PC did not answer through the hub")
            return waiter["sock"]
        finally:
            with self.lock:
                self.pending.pop(token, None)


def _service_field(value):
    if not isinstance(value, dict):
        return None
    ver = value.get("ver")
    state = value.get("state")
    if state not in ("running", "stopped"):
        return {}
    return {"ver": ver if isinstance(ver, str) and re.fullmatch(r"\d{1,4}\.\d{1,4}\.\d{1,4}", ver) else "",
            "state": state}


def server_address_for(host, port, source_ip, timeout=3):
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            send_line(s, {"op": "where"})
            reply = json.loads(read_line(s, limit=1 << 16))
    except (OSError, ValueError, ConnectionError):
        return None
    ips = [ip for ip in reply.get("ips", []) if isinstance(ip, str)] if reply.get("ok") else []
    from network_info import is_tailscale
    if is_tailscale(source_ip):
        same = [ip for ip in ips if is_tailscale(ip)]
    else:
        prefix = source_ip.rsplit(".", 1)[0] + "."
        same = [ip for ip in ips if ip.startswith(prefix)]
    return (same[0], port) if same else None


_last_answered = {}


def viewer_servers(text):
    return list(dict.fromkeys(clean_hubs(text) + VIEWER_SERVERS))


def fetch_online_any(servers, timeout=3):
    key = tuple(servers)
    order = sorted(servers, key=lambda s: s != _last_answered.get(key))
    error = ConnectionError("no AnyKir Server set")
    for server in order:
        try:
            hosts = fetch_online(server, timeout=timeout)
        except (OSError, ValueError, ConnectionError) as e:
            error = e
            continue
        _last_answered[key] = server
        return server, hosts
    raise error


def fetch_online(server, timeout=3):
    host, port = parse_addr(server, HUB_PORT)
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        send_line(s, {"op": "list"})
        reply = json.loads(read_line(s, limit=1 << 20))
    return reply.get("hosts", []) if reply.get("ok") else []


def connect_via(server, hid, timeout=15):
    host, port = parse_addr(server, HUB_PORT)
    s = socket.create_connection((host, port), timeout=timeout)
    try:
        tune(s)
        s.settimeout(timeout)
        send_line(s, {"op": "connect", "id": hid})
        reply = json.loads(read_line(s))
        if not reply.get("ok"):
            raise ConnectionError(reply.get("err") or "the server refused")
        return s
    except (OSError, ValueError, ConnectionError):
        s.close()
        raise


def match(hosts, address):
    try:
        wanted = parse_addr(address, 7001)[0].strip().lower()
    except ValueError:
        wanted = address.strip().lower()
    for e in hosts:
        if wanted and (wanted == e.get("peer") or wanted in e.get("ips", []) or wanted == str(e.get("name", "")).lower()):
            return e
    return None


def call_home(hub, hello, serve, alive, ips=None, active=None, on_stop=None, source=None):
    active = active or (lambda: True)
    host, port = parse_addr(hub, HUB_PORT)
    holder = {"sock": None, "refused": False}

    target = {"addr": (host, port)}

    def connect(timeout):
        ip = source() if source else ""
        if not ip:
            return socket.create_connection((host, port), timeout=timeout)
        try:
            return socket.create_connection(target["addr"], timeout=timeout, source_address=(ip, 0))
        except OSError:
            alt = server_address_for(host, port, ip)
            if not alt or alt == target["addr"]:
                raise
            target["addr"] = alt
            return socket.create_connection(alt, timeout=timeout, source_address=(ip, 0))

    def loop():
        while alive():
            s = None
            was_up = refused = False
            try:
                if not active():
                    raise ConnectionError("not hosting")
                s = connect(3)
                was_up = True
                holder["sock"] = s
                tune(s)
                s.settimeout(PING_EVERY * 3)
                send_line(s, dict(hello(), op="hello", ips=ips() if ips else []))
                while alive() and active():
                    msg = json.loads(read_line(s))
                    if msg.get("op") == "refused":
                        refused = True
                        break
                    if msg.get("op") == "stop":
                        refused = True
                        if on_stop:
                            threading.Thread(target=on_stop, daemon=True).start()
                        break
                    if msg.get("op") == "ping":
                        send_line(s, {"op": "pong"})
                    elif msg.get("op") == "open":
                        threading.Thread(target=_session, args=(str(msg.get("token", ""))[:64],
                                                                str(msg.get("from") or "")[:64]),
                                         daemon=True).start()
            except (OSError, ValueError, ConnectionError):
                pass
            finally:
                if s:
                    s.close()
                holder["sock"] = None
            holder["refused"] = refused
            for _ in range(REFUSED_WAIT * 2 if refused else 2 if was_up else RETRY_EVERY * 2):
                if not alive():
                    return
                time.sleep(0.5)

    def _session(token, viewer_ip=""):
        try:
            s = connect(10)
            tune(s)
            send_line(s, {"op": "session", "token": token})
            serve(s, "AnyKir Server", viewer_ip)
        except OSError:
            pass

    threading.Thread(target=loop, daemon=True).start()
    return holder
