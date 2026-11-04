import argparse
import ctypes
import io
import json
import os
import platform
import secrets
import socket
import string
import struct
import subprocess
import sys
import threading
import time

import service_setup
import updater
import windows_desktop
from desktop_capture import DesktopCapture
from network_info import is_tailscale, kill_process, local_ips, port_owners, status_text
from common import (APP, CHUNK, VERSION, acquire_single_instance, config_dir, show_hook, DEFAULT_DIRECT_PORT, DEFAULT_RELAY_PORT, T_CHUNK, T_FRAME, T_JSON,
                    AuthError, derive_key, load_json, parse_addr, read_line, save_json, send_line,
                    server_handshake, tune)


def set_dpi_aware():
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


set_dpi_aware()

import mss  # noqa: E402  (must come after DPI awareness so coordinates are real pixels)
import numpy as np  # noqa: E402
import pyperclip  # noqa: E402
from PIL import Image  # noqa: E402
from pynput import keyboard, mouse  # noqa: E402

SPECIAL_KEYS = {
    "Return": "enter", "KP_Enter": "enter", "BackSpace": "backspace", "Tab": "tab", "ISO_Left_Tab": "tab",
    "Escape": "esc", "space": "space", "Delete": "delete", "Insert": "insert", "Home": "home", "End": "end",
    "Prior": "page_up", "Next": "page_down", "Left": "left", "Right": "right", "Up": "up", "Down": "down",
    "Shift_L": "shift", "Shift_R": "shift_r", "Control_L": "ctrl", "Control_R": "ctrl_r",
    "Alt_L": "alt", "Alt_R": "alt_r", "Super_L": "cmd", "Super_R": "cmd_r", "Win_L": "cmd", "Win_R": "cmd_r",
    "Caps_Lock": "caps_lock", "Num_Lock": "num_lock", "Scroll_Lock": "scroll_lock",
    "Print": "print_screen", "Pause": "pause", "Menu": "menu",
    "ctrl": "ctrl", "alt": "alt", "shift": "shift", "cmd": "cmd", "esc": "esc", "tab": "tab", "enter": "enter",
    "delete": "delete", "f4": "f4", "print_screen": "print_screen",
}
for _i in range(1, 25):
    SPECIAL_KEYS[f"F{_i}"] = f"f{_i}"

MSS = getattr(mss, "MSS", None) or mss.mss

BUTTONS = {"left": mouse.Button.left, "right": mouse.Button.right, "middle": mouse.Button.middle}


class Injector:

    def __init__(self):
        self.mouse = mouse.Controller()
        self.kb = keyboard.Controller()
        self.geom = (0, 0, 1, 1)
        self.held_keys, self.held_buttons = set(), set()
        self.enabled = True

    def _resolve(self, keysym, char):
        name = SPECIAL_KEYS.get(keysym)
        if name:
            key = getattr(keyboard.Key, name, None)
            if key is not None:
                return key
        if len(keysym) == 1:
            return keyboard.KeyCode.from_char(keysym)
        if char and len(char) == 1 and char.isprintable():
            return keyboard.KeyCode.from_char(char)
        return None

    def on_mouse(self, m):
        if not self.enabled:
            return
        left, top, w, h = self.geom
        x = left + min(max(m["x"], 0.0), 1.0) * (w - 1)
        y = top + min(max(m["y"], 0.0), 1.0) * (h - 1)
        self.mouse.position = (int(x), int(y))
        btn = BUTTONS.get(m.get("b"))
        if btn and m.get("a") == "down":
            self.mouse.press(btn)
            self.held_buttons.add(btn)
        elif btn and m.get("a") == "up":
            self.mouse.release(btn)
            self.held_buttons.discard(btn)

    def on_wheel(self, m):
        if self.enabled:
            self.mouse.scroll(int(m.get("dx", 0)), int(m.get("dy", 0)))

    def on_key(self, m):
        if not self.enabled:
            return
        key = self._resolve(m.get("k", ""), m.get("c", ""))
        if key is None:
            return
        if m.get("a") == "down":
            self.kb.press(key)
            self.held_keys.add(key)
        else:
            self.kb.release(key)
            self.held_keys.discard(key)

    def on_combo(self, m):
        if not self.enabled:
            return
        keys = [k for k in (self._resolve(n, "") for n in m.get("keys", [])) if k is not None]
        for k in keys:
            self.kb.press(k)
        for k in reversed(keys):
            self.kb.release(k)

    def release_all(self):
        for k in list(self.held_keys):
            try:
                self.kb.release(k)
            except Exception:
                pass
        for b in list(self.held_buttons):
            try:
                self.mouse.release(b)
            except Exception:
                pass
        self.held_keys.clear()
        self.held_buttons.clear()


CURSOR_SHAPES = {32512: "arrow", 32513: "ibeam", 32514: "wait", 32515: "cross", 32516: "up", 32642: "size_nwse",
                 32643: "size_nesw", 32644: "size_we", 32645: "size_ns", 32646: "size_all", 32648: "no",
                 32649: "hand", 32650: "busy", 32651: "help"}


class _CursorInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("flags", ctypes.c_uint32), ("hCursor", ctypes.c_void_p),
                ("x", ctypes.c_long), ("y", ctypes.c_long)]


class WinCursor:

    def __init__(self):
        self.user32 = ctypes.windll.user32
        self.user32.LoadCursorW.restype = ctypes.c_void_p
        self.user32.LoadCursorW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.handles = {self.user32.LoadCursorW(None, i): name for i, name in CURSOR_SHAPES.items()}

    def read(self):
        info = _CursorInfo()
        info.cbSize = ctypes.sizeof(info)
        if not self.user32.GetCursorInfo(ctypes.byref(info)):
            return None
        return bool(info.flags & 1), info.x, info.y, self.handles.get(info.hCursor, "arrow")


def _is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


IS_ADMIN = sys.platform == "win32" and _is_admin()


def foreground_blocked():
    if sys.platform != "win32" or IS_ADMIN:
        return False
    u, k, a = ctypes.windll.user32, ctypes.windll.kernel32, ctypes.windll.advapi32
    u.GetForegroundWindow.restype = ctypes.c_void_p
    u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    k.OpenProcess.restype = ctypes.c_void_p
    k.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    a.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)]
    a.GetTokenInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong,
                                      ctypes.POINTER(ctypes.c_ulong)]
    hwnd = u.GetForegroundWindow()
    if not hwnd:
        return False
    pid = ctypes.c_ulong()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    proc = k.OpenProcess(0x1000, False, pid.value)
    if not proc:
        return True
    try:
        tok = ctypes.c_void_p()
        if not a.OpenProcessToken(proc, 0x8, ctypes.byref(tok)):
            return True
        try:
            elevated, size = ctypes.c_ulong(), ctypes.c_ulong()
            if a.GetTokenInformation(tok, 20, ctypes.byref(elevated), ctypes.sizeof(elevated),
                                     ctypes.byref(size)):
                return bool(elevated.value)
            return False
        finally:
            k.CloseHandle(tok)
    finally:
        k.CloseHandle(proc)


MAX_IN_FLIGHT = 6


class Grabber:

    def __init__(self):
        self.native = DesktopCapture() if sys.platform == "win32" and windows_desktop.enabled else None
        self.sct = None if self.native else MSS()
        self.cam = None
        self.dx_ok = sys.platform == "win32" and not windows_desktop.enabled
        self.dx_retry = 0.0
        self.name = "win32" if self.native else "gdi"

    def close(self):
        self._drop_cam()
        if self.native:
            self.native.close()
        if self.sct:
            self.sct.close()

    def _drop_cam(self):
        if self.cam is not None:
            try:
                self.cam.release()
            except Exception:
                pass
            self.cam = None

    def grab(self, idx):
        if self.native:
            return self.native.grab(idx)
        mons = self.sct.monitors
        idx = min(max(idx, 0), len(mons) - 1)
        mon, count = mons[idx], len(mons) - 1
        if self.dx_ok and idx == 1 and time.time() >= self.dx_retry:
            try:
                if self.cam is None:
                    import dxcam
                    self.cam = dxcam.create(output_color="BGRA")
                    self.name = "dxgi"
                frame = self.cam.grab()
                if frame is None:
                    return None, mon, count
                h, w = frame.shape[:2]
                if (w, h) != (mon["width"], mon["height"]):
                    raise RuntimeError("DXGI output does not match the monitor")
                return frame.view(np.uint32).reshape(h, w), mon, count
            except Exception:
                self.dx_retry = time.time() + 3
                self._drop_cam()
        elif self.cam is not None:
            self._drop_cam()
        self.name = "gdi"
        shot = self.sct.grab(mon)
        w, h = shot.size
        return np.frombuffer(shot.bgra, dtype=np.uint32).reshape(h, w), mon, count


def changed_rects(arr, prev, gap=48, max_rects=4):
    diff = arr != prev
    rows = np.flatnonzero(diff.any(axis=1))
    if rows.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(rows) > gap)
    starts, ends = np.concatenate(([0], breaks + 1)), np.concatenate((breaks, [rows.size - 1]))
    bands = [(int(rows[a]), int(rows[b]) + 1) for a, b in zip(starts, ends)]
    if len(bands) > max_rects:
        bands = [(bands[0][0], bands[-1][1])]
    out = []
    for y0, y1 in bands:
        cols = np.flatnonzero(diff[y0:y1].any(axis=0))
        out.append((int(cols[0]), y0, int(cols[-1]) + 1, y1))
    return out


def secure_desktop_active():
    if sys.platform != "win32":
        return False
    u = ctypes.windll.user32
    u.OpenInputDesktop.restype = ctypes.c_void_p
    u.OpenInputDesktop.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    u.CloseDesktop.argtypes = [ctypes.c_void_p]
    desktop = u.OpenInputDesktop(0, False, 0x0100)
    if not desktop:
        return True
    u.CloseDesktop(desktop)
    return False


class HostSession:

    def __init__(self, ch, service, peer):
        self.ch, self.service, self.peer = ch, service, peer
        self.stop = threading.Event()
        self.cfg = {"fps": 60, "quality": 65, "scale": 1.0, "monitor": 1}
        self.cfg_lock = threading.Lock()
        self.force_full = threading.Event()
        self.frame, self.frame_cv = None, threading.Condition()
        self.active_until = 0.0
        self.flow = threading.Condition()
        self.flow_on, self.sent, self.acked = False, 0, 0
        self.injector = Injector()
        self.injector.enabled = service.cfg.get("allow_input", True)
        self.upload = None
        self.upd = updater.UpdateReceiver()
        self.cancel = threading.Event()
        self.last_clip = None
        self.input_desktop = windows_desktop.DesktopBinding()

    def run(self):
        threading.Thread(target=self._grab_loop, daemon=True).start()
        threading.Thread(target=self._encode_loop, daemon=True).start()
        threading.Thread(target=self._clipboard_loop, daemon=True).start()
        threading.Thread(target=self._cursor_loop, daemon=True).start()
        try:
            kept = updater.staged()
            self.ch.send_json({"t": "hello", "host": socket.gethostname(), "os": platform.platform(),
                               "input": self.injector.enabled, "ver": VERSION,
                               "admin": IS_ADMIN, "uac": windows_desktop.enabled,
                               "files": self.service.cfg.get("allow_files", True),
                               "clipboard": self.service.cfg.get("allow_clipboard", True),
                               "upd": not windows_desktop.enabled,
                               "rel": kept["version"] if kept else None})
            while not self.stop.is_set():
                mtype, body = self.ch.recv()
                if mtype == T_JSON:
                    try:
                        self._handle(json.loads(body))
                    except OSError:
                        raise
                    except Exception:
                        pass
                elif mtype == T_CHUNK:
                    if self.upd.active:
                        try:
                            self.upd.chunk(body)
                        except ValueError as e:
                            self.ch.send_json({"t": "upd_done", "ok": False, "err": str(e)})
                    elif self.upload:
                        self.upload.write(body)
        except Exception:
            pass
        finally:
            self.close()
            try:
                self.input_desktop.close()
            except OSError:
                pass

    def close(self):
        self.stop.set()
        for cv in (self.frame_cv, self.flow):
            with cv:
                cv.notify_all()
        self.cancel.set()
        self.injector.release_all()
        self.upd.abort()
        if self.upload:
            try:
                self.upload.close()
            except OSError:
                pass
        self.ch.close()

    def _handle(self, m):
        t = m.get("t")
        if t in ("mouse", "wheel", "key", "combo", "lock"):
            try:
                self.input_desktop.sync(before_switch=self.injector.release_all)
            except OSError:
                return
            self.active_until = time.time() + 1.5
        if t == "clip" and not self.service.cfg.get("allow_clipboard", True):
            return
        if t in ("ls", "get", "put", "put_end") and not self.service.cfg.get("allow_files", True):
            reply = {"ls": "ls_result", "get": "file_end", "put": "put_done", "put_end": "put_done"}[t]
            self.ch.send_json({"t": reply, "ok": False, "err": "File transfer is disabled on this host."})
            return
        if t == "ack":
            with self.flow:
                self.acked = max(self.acked, int(m.get("n", 0)))
                self.flow.notify_all()
        elif t == "caps":
            self.flow_on = bool(m.get("ack"))
        elif t == "mouse":
            self.injector.on_mouse(m)
        elif t == "wheel":
            self.injector.on_wheel(m)
        elif t == "key":
            self.injector.on_key(m)
        elif t == "combo":
            self.injector.on_combo(m)
        elif t == "lock":
            if self.injector.enabled and sys.platform == "win32":
                ctypes.windll.user32.LockWorkStation()
        elif t == "cfg":
            with self.cfg_lock:
                for k in ("fps", "quality", "scale", "monitor"):
                    if k in m:
                        self.cfg[k] = m[k]
            self.force_full.set()
        elif t == "refresh":
            self.force_full.set()
        elif t == "clip":
            text = str(m.get("text", ""))[:1_000_000]
            self.last_clip = text
            try:
                pyperclip.copy(text)
            except Exception:
                pass
        elif t == "ls":
            self._list_dir(m.get("path", ""))
        elif t == "get":
            threading.Thread(target=self._send_file, args=(m.get("path", ""),), daemon=True).start()
        elif t == "put":
            self._begin_upload(m)
        elif t == "put_end":
            self._end_upload()
        elif t == "cancel":
            self.cancel.set()
        elif t == "upd_get":
            threading.Thread(target=self._send_update, daemon=True).start()
        elif t == "upd_start":
            self._begin_update(m)
        elif t == "upd_end":
            self._end_update()

    def _send_update(self):
        try:
            updater.send_release(self.ch, self.stop)
        except OSError:
            pass

    def _begin_update(self, m):
        try:
            if windows_desktop.enabled or not self.service.cfg.get("allow_update", True):
                raise ValueError("updates are turned off on this PC")
            self.upd.start(m)
        except (ValueError, OSError) as e:
            self.ch.send_json({"t": "upd_done", "ok": False, "err": str(e)})

    def _end_update(self):
        err = self.upd.finish()
        self.ch.send_json({"t": "upd_done", "ok": err is None, "err": err})
        if err is None:
            threading.Timer(1.0, updater.restart_now).start()

    def _list_dir(self, path):
        reply = {"t": "ls_result", "path": path, "entries": [], "err": None}
        try:
            if not path:
                if sys.platform == "win32":
                    drives = [f"{c}:\\" for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")]
                    reply["entries"] = [[d, True, 0, 0] for d in drives]
                    reply["path"] = ""
                    self.ch.send_json(reply)
                    return
                path = os.path.expanduser("~")
            path = os.path.abspath(path)
            reply["path"] = path
            entries = []
            with os.scandir(path) as it:
                for e in it:
                    try:
                        st = e.stat()
                        entries.append([e.name, e.is_dir(), 0 if e.is_dir() else st.st_size, int(st.st_mtime)])
                    except OSError:
                        entries.append([e.name, False, 0, 0])
                    if len(entries) >= 5000:
                        break
            entries.sort(key=lambda x: (not x[1], x[0].lower()))
            reply["entries"] = entries
        except OSError as e:
            reply["err"] = str(e)
        self.ch.send_json(reply)

    def _send_file(self, path):
        self.cancel.clear()
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                self.ch.send_json({"t": "file_start", "name": os.path.basename(path), "size": size})
                while not self.stop.is_set() and not self.cancel.is_set():
                    data = f.read(CHUNK)
                    if not data:
                        break
                    self.ch.send(T_CHUNK, data)
            self.ch.send_json({"t": "file_end", "ok": not self.cancel.is_set()})
        except OSError as e:
            try:
                self.ch.send_json({"t": "file_end", "ok": False, "err": str(e)})
            except OSError:
                pass

    def _begin_upload(self, m):
        name = os.path.basename(str(m.get("name", "upload.bin"))) or "upload.bin"
        folder = m.get("dir") or os.path.join(os.path.expanduser("~"), "Downloads", APP)
        try:
            os.makedirs(folder, exist_ok=True)
            self.upload_path = os.path.join(folder, name)
            self.upload = open(self.upload_path, "wb")
        except OSError as e:
            self.upload = None
            self.ch.send_json({"t": "put_done", "ok": False, "err": str(e)})

    def _end_upload(self):
        if self.upload:
            self.upload.close()
            self.upload = None
            self.ch.send_json({"t": "put_done", "ok": True, "path": self.upload_path})

    def _clipboard_loop(self):
        if not self.service.cfg.get("allow_clipboard", True):
            return
        while not self.stop.wait(0.8):
            try:
                text = pyperclip.paste()
            except Exception:
                continue
            if text and text != self.last_clip and len(text) < 1_000_000:
                self.last_clip = text
                try:
                    self.ch.send_json({"t": "clip", "text": text})
                except OSError:
                    return

    def _cursor_loop(self):
        if sys.platform != "win32":
            return
        desktop = windows_desktop.DesktopBinding()
        try:
            cur = WinCursor()
        except Exception:
            return
        last, last_warn, next_check = None, None, 0.0
        while not self.stop.wait(0.016):
            try:
                try:
                    desktop.sync()
                except OSError:
                    continue
                r = cur.read()
                if r:
                    vis, x, y, shape = r
                    left, top, w, h = self.injector.geom
                    state = (vis, round((x - left) / max(w - 1, 1), 4), round((y - top) / max(h - 1, 1), 4), shape)
                    if state != last:
                        last = state
                        self.ch.send_json({"t": "cur", "vis": vis, "x": state[1], "y": state[2], "s": shape})
                now = time.time()
                if now >= next_check:
                    next_check = now + 1
                    warn = (foreground_blocked(), secure_desktop_active())
                    if warn != last_warn:
                        last_warn = warn
                        self.ch.send_json({"t": "warn", "blocked": warn[0], "locked": warn[1]})
            except OSError:
                break
            except Exception:
                pass
        try:
            desktop.close()
        except OSError:
            pass

    def _grab_loop(self):
        grabber, last = None, None
        capture_state = None
        desktop = windows_desktop.DesktopBinding()

        def release_capture():
            nonlocal grabber
            if grabber is not None:
                old, grabber = grabber, None
                try:
                    old.close()
                except Exception:
                    pass

        try:
            while not self.stop.is_set():
                t0 = time.time()
                with self.cfg_lock:
                    cfg = dict(self.cfg)
                try:
                    if desktop.sync(before_switch=release_capture):
                        last = None
                        with self.frame_cv:
                            self.frame = None
                        self.force_full.set()
                    if grabber is None:
                        grabber = Grabber()
                    pix, mon, count = grabber.grab(int(cfg["monitor"]))
                except Exception as exc:
                    release_capture()
                    last = None
                    self.force_full.set()
                    state = str(exc)
                    if capture_state != state:
                        capture_state = state
                        self.ch.send_json({"t": "capture_status", "ok": False, "err": state[:300]})
                    self.stop.wait(0.5)
                    continue
                if capture_state is not None:
                    self.ch.send_json({"t": "capture_status", "ok": True})
                    capture_state = None
                if pix is None and last is not None and self.force_full.is_set():
                    pix = last
                if pix is not None:
                    last = pix
                    with self.frame_cv:
                        self.frame = (pix, mon, count, cfg, grabber.name)
                        self.frame_cv.notify()
                if grabber.name == "dxgi":
                    interval = 1 / 120
                elif time.time() < self.active_until:
                    interval = 1 / max(1, min(int(cfg["fps"]), 60))
                else:
                    interval = 1 / 10
                delay = interval - (time.time() - t0)
                if delay > 0:
                    time.sleep(delay)
        finally:
            release_capture()
            try:
                desktop.close()
            except OSError:
                pass
            self.stop.set()

    def _wait_window(self):
        if not self.flow_on:
            return
        t0 = time.time()
        with self.flow:
            while (self.sent - self.acked > MAX_IN_FLIGHT and not self.stop.is_set()
                   and time.time() - t0 < 2):
                self.flow.wait(0.05)

    def _encode_loop(self):
        prev = prev_key = None
        try:
            while not self.stop.is_set():
                self._wait_window()
                with self.frame_cv:
                    if self.frame is None:
                        self.frame_cv.wait(0.1)
                    frame, self.frame = self.frame, None
                if frame is None:
                    continue
                t0 = time.time()
                raw, mon, count, cfg, cap = frame
                try:
                    idx = min(max(int(cfg["monitor"]), 0), count)
                    scale = min(max(float(cfg["scale"]), 0.25), 1.0)
                    h, w = raw.shape
                    if scale < 1.0:
                        img = Image.frombuffer("RGB", (w, h), raw, "raw", "BGRX", 0, 1)
                        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.BILINEAR)
                        arr = np.frombuffer(img.tobytes("raw", "BGRX"), dtype=np.uint32).reshape(img.height, img.width)
                    else:
                        arr = raw
                    fh, fw = arr.shape

                    key = (idx, scale, fw, fh)
                    if key != prev_key:
                        self.injector.geom = (mon["left"], mon["top"], mon["width"], mon["height"])
                        self.ch.send_json({"t": "screen", "w": fw, "h": fh, "count": count, "mon": idx, "cap": cap})
                        prev, prev_key = None, key
                    if self.force_full.is_set():
                        self.force_full.clear()
                        prev = None

                    rects = [(0, 0, fw, fh)] if prev is None else changed_rects(arr, prev)
                    if not rects:
                        continue
                    quality = int(min(max(cfg["quality"], 10), 95))
                    for x0, y0, x1, y1 in rects:
                        region = np.ascontiguousarray(arr[y0:y1, x0:x1])
                        im = Image.frombuffer("RGB", (x1 - x0, y1 - y0), region, "raw", "BGRX", 0, 1)
                        buf = io.BytesIO()
                        im.save(buf, "JPEG", quality=quality)
                        self.ch.send(T_FRAME, struct.pack(">HHHH", x0, y0, x1 - x0, y1 - y0) + buf.getvalue())
                    prev = arr
                    with self.flow:
                        self.sent += len(rects)
                    self.active_until = max(self.active_until, time.time() + 1.0)
                except OSError:
                    return
                except Exception:
                    prev = None
                    time.sleep(0.2)
                    continue
                delay = 1.0 / max(1, min(int(cfg["fps"]), 60)) - (time.time() - t0)
                if delay > 0:
                    time.sleep(delay)
        finally:
            self.stop.set()


class HostService:

    def __init__(self, cfg):
        self.cfg = cfg
        self.key = derive_key(cfg["password"], cfg["id"])
        self.status = "Starting..."
        self.session = None
        self.fails = []
        self.stopping = False
        self._lock = threading.Lock()
        self._relay_sock = None
        self._listener = None
        self._relay_gen = 0
        self.port_blocked = None
        self._service_seen = 0.0

    def set_password(self, password):
        self.cfg["password"] = password
        self.key = derive_key(password, self.cfg["id"])
        save_json("host.json", self.cfg)

    def start(self):
        self.restart_network()
        updater.ensure_server()
        if not windows_desktop.enabled:
            updater.Watcher(self._update_ready).start()

    def _update_ready(self):
        if not windows_desktop.enabled and not self.session:
            updater.restart_now()

    def restart_network(self):
        self._relay_gen += 1
        for s in (self._relay_sock, self._listener):
            if s:
                try:
                    s.close()
                except OSError:
                    pass
        self._relay_sock = self._listener = None
        if self.cfg.get("relay"):
            threading.Thread(target=self._relay_loop, args=(self._relay_gen,), daemon=True).start()
        else:
            self.status = "Ready - waiting for connection"
        if self.cfg.get("listen"):
            threading.Thread(target=self._listen_loop, args=(self._relay_gen,), daemon=True).start()

    def disconnect_viewer(self):
        s = self.session
        if s:
            s.close()

    def stop(self):
        self.stopping = True
        self._relay_gen += 1
        self.disconnect_viewer()
        for sock in (self._listener, self._relay_sock):
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass

    def _relay_loop(self, gen):
        backoff = 2
        while not self.stopping and gen == self._relay_gen:
            if self.yield_to_service():
                self.status = "Shared through the admin service (same ID and password)"
                time.sleep(5)
                continue
            try:
                addr = parse_addr(self.cfg["relay"], DEFAULT_RELAY_PORT)
                self.status = f"Connecting to relay {addr[0]}..."
                s = socket.create_connection(addr, timeout=10)
                tune(s)
                self._relay_sock = s
                send_line(s, {"op": "host", "id": self.cfg["id"], "sec": self.cfg["secret"],
                              "key": self.cfg.get("relay_key", "")})
                reply = json.loads(read_line(s))
                if not reply.get("ok"):
                    raise ConnectionError(reply.get("err", "relay refused"))
                s.settimeout(90)
                self._set_idle_status()
                backoff = 2
                while True:
                    msg = json.loads(read_line(s))
                    if msg.get("op") == "incoming":
                        threading.Thread(target=self._accept_from_relay, args=(msg["token"], gen), daemon=True).start()
            except Exception as e:
                if gen != self._relay_gen:
                    return
                self.status = f"Offline ({e}); retrying..."
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)

    def _accept_from_relay(self, token, gen):
        try:
            s = socket.create_connection(parse_addr(self.cfg["relay"], DEFAULT_RELAY_PORT), timeout=10)
            tune(s)
            send_line(s, {"op": "accept", "token": token, "key": self.cfg.get("relay_key", "")})
            if not json.loads(read_line(s)).get("ok"):
                s.close()
                return
            self.serve(s, "relay")
        except Exception:
            pass

    SERVICE_GRACE = 90

    def yield_to_service(self):
        if windows_desktop.enabled or sys.platform != "win32":
            return False
        now = time.time()
        if service_setup.state() == "running" and service_setup.installed_version():
            self._service_seen = now
            return True
        return now - self._service_seen < self.SERVICE_GRACE

    def _listen_loop(self, gen):
        port = int(self.cfg["listen"])
        while not self.stopping and gen == self._relay_gen:
            if self.yield_to_service():
                self.port_blocked = None
                if not self.cfg.get("relay"):
                    self.status = f"Shared through the admin service on port {port} (same password)"
                time.sleep(5)
                continue
            srv = socket.socket()
            try:
                if sys.platform == "win32":
                    # Never share the port: with SO_REUSEADDR Windows splits connections between listeners.
                    srv.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", -5), 1)
                else:
                    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.bind(("0.0.0.0", port))
                srv.listen(5)
            except OSError:
                srv.close()
                self.port_blocked = port_owners(port)
                if not self.cfg.get("relay"):
                    self.status = f"Port {port} is in use by another program - retrying"
                time.sleep(5)
                continue
            if gen != self._relay_gen or self.stopping:
                srv.close()
                return
            self._listener = srv
            if self.port_blocked is not None or self.status.startswith("Shared"):
                self.port_blocked = None
                if not self.cfg.get("relay"):
                    self.status = "Ready - waiting for connection"
            srv.settimeout(5)
            try:
                while not self.stopping and gen == self._relay_gen:
                    try:
                        s, addr = srv.accept()
                    except socket.timeout:
                        if self.yield_to_service():
                            break
                        continue
                    s.settimeout(None)
                    tune(s)
                    threading.Thread(target=self.serve, args=(s, addr[0]), daemon=True).start()
                else:
                    return
            except OSError:
                return
            srv.close()
            self._listener = None

    def _set_idle_status(self):
        if not self.session:
            self.status = "Online - waiting for connection"

    def _locked_out(self):
        now = time.time()
        self.fails = [t for t in self.fails if now - t < 300]
        return len(self.fails) >= 5

    def serve(self, sock, label):
        with self._lock:
            if self.session or self._locked_out():
                sock.close()
                return
        try:
            sock.settimeout(10)
            ch = server_handshake(sock, self.key, self.cfg["id"])
            sock.settimeout(None)
        except (AuthError, OSError, ConnectionError):
            self.fails.append(time.time())
            time.sleep(1)
            sock.close()
            return
        with self._lock:
            if self.session:
                ch.close()
                return
            session = HostSession(ch, self, label)
            self.session = session
        self.status = f"Connected: viewer via {label}"
        print(f"[{time.strftime('%H:%M:%S')}] viewer connected via {label}", flush=True)
        try:
            session.run()
        finally:
            self.session = None
            self._set_idle_status()
            print(f"[{time.strftime('%H:%M:%S')}] viewer disconnected", flush=True)
            if updater.update_waiting():
                threading.Timer(3.0, self._update_ready).start()


def new_password():
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(12))


def load_config():
    cfg = load_json("host.json")
    changed = False
    defaults = {
        "id": "".join(secrets.choice(string.digits) for _ in range(9)),
        "secret": secrets.token_hex(24),
        "password": new_password(),
        "relay": "", "relay_key": "", "listen": DEFAULT_DIRECT_PORT, "allow_input": True, "allow_update": True,
    }
    for k, v in defaults.items():
        if k not in cfg:
            cfg[k] = v
            changed = True
    if not cfg.get("relay") and not cfg.get("listen"):
        cfg["listen"] = DEFAULT_DIRECT_PORT
        changed = True
    if changed:
        save_json("host.json", cfg)
    return cfg


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
            winreg.SetValueEx(k, APP + "Host", 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, APP + "Host")
            except FileNotFoundError:
                pass


def get_autostart():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            winreg.QueryValueEx(k, APP + "Host")
            return True
    except Exception:
        return False


class HostApp:
    def __init__(self, root, service):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk, self.root, self.svc = tk, ttk, root, service
        cfg = service.cfg
        self._keep = []
        root.title(f"{APP} - Share this PC")
        root.resizable(False, False)
        f = ttk.Frame(root, padding=20)
        f.pack()

        port = cfg.get("listen") or DEFAULT_DIRECT_PORT
        addresses = local_ips()
        ips = [f"{ip}:{port}" for ip in addresses]
        self.addr = ips[0] if ips else ""
        ttk.Label(f, text="To connect, enter this on your other PC:", font=("Segoe UI", 10)).pack(anchor="w")

        def field(label, value, copy_value, extra=None):
            r = ttk.Frame(f)
            r.pack(fill="x", pady=(10, 0))
            ttk.Label(r, text=label, width=10).pack(side="left")
            var = tk.StringVar(value=value)
            self._keep.append(var)  # Tk drops a variable when Python garbage-collects it
            ttk.Label(r, textvariable=var, font=("Consolas", 15, "bold")).pack(side="left", padx=6)
            ttk.Button(r, text="Copy", width=6, command=lambda: self._copy(copy_value())).pack(side="right")
            if extra:
                ttk.Button(r, text=extra[0], width=8, command=extra[1]).pack(side="right", padx=(0, 4))
            return var

        self.addr_var = field("Address", self.addr or "(no network)", lambda: self.addr)
        self.pw_var = field("Password", cfg["password"], lambda: self.svc.cfg["password"],
                            extra=("Change", self._set_pw))
        note, color = status_text(addresses)
        self.network_note = ttk.Label(f, text=note, foreground=color, wraplength=340)
        self.network_note.pack(anchor="w", pady=(8, 0))
        root.after(15000, self._refresh_network)
        ttk.Label(f, text=f"{APP} v{VERSION}", foreground="#777").pack(anchor="e")

        ttk.Separator(f).pack(fill="x", pady=14)
        self.status_var = tk.StringVar()
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Label(row, textvariable=self.status_var, wraplength=250).pack(side="left")
        self.kick = ttk.Button(row, text="Disconnect", command=service.disconnect_viewer)
        self.kick.pack(side="right")
        self.free_btn = ttk.Button(row, text="Free port", command=self._free_port)
        self._blocked_seen = None
        self._shared_by_service = False

        if sys.platform == "win32":
            self.auto_var = tk.BooleanVar(value=get_autostart())
            ttk.Checkbutton(f, text="Start automatically with Windows", variable=self.auto_var,
                            command=lambda: set_autostart(self.auto_var.get())).pack(anchor="w", pady=(12, 0))

        self.adv = ttk.Frame(f)
        self.adv_open = False
        self.adv_btn = ttk.Button(f, text="Advanced settings", command=self._toggle_adv)
        self.adv_btn.pack(anchor="w", pady=(10, 0))
        ttk.Button(self.adv, text="Make a new password", command=self._new_pw).pack(anchor="w", pady=4)
        self.input_var = tk.BooleanVar(value=cfg["allow_input"])
        ttk.Checkbutton(self.adv, text="Allow remote control (untick = view only)", variable=self.input_var,
                        command=self._save_input).pack(anchor="w", pady=2)
        self.relay_var = tk.StringVar(value=cfg["relay"])
        self.rkey_var = tk.StringVar(value=cfg["relay_key"])
        self.listen_var = tk.StringVar(value=str(cfg["listen"] or ""))
        self.usrc_var = tk.StringVar(value=", ".join(updater.user_sources()))
        for label, var in (("Relay server (optional)", self.relay_var), ("Relay key", self.rkey_var),
                           ("Port", self.listen_var), ("Update source (optional)", self.usrc_var)):
            r = ttk.Frame(self.adv)
            r.pack(fill="x", pady=2)
            ttk.Label(r, text=label, width=22).pack(side="left")
            ttk.Entry(r, textvariable=var, width=20).pack(side="left")
        ttk.Button(self.adv, text="Save and reconnect", command=self._save_net).pack(anchor="e", pady=4)

        self.svc_note = ttk.Label(f, foreground="#a15c00", wraplength=340)
        self.svc_btn = None
        if service_setup.available() or service_setup.state() is not None:
            ttk.Separator(self.adv).pack(fill="x", pady=8)
            r = ttk.Frame(self.adv)
            r.pack(fill="x", pady=2)
            self.svc_label = ttk.Label(r, wraplength=170)
            self.svc_label.pack(side="left")
            self.svc_btn = ttk.Button(r, command=self._toggle_service)
            self.svc_btn.pack(side="right")
            self.svc_reinstall = ttk.Button(r, text="Reinstall", command=lambda: self._toggle_service(reinstall=True))
        self._show_service_state()

        self.tray = self._make_tray()
        show_hook["fn"] = lambda: root.after(0, self.show)
        updater.quit_hook["fn"] = lambda: root.after(0, self._quit_for_update)
        root.protocol("WM_DELETE_WINDOW", self._close_window)
        self._tick()

    def _make_tray(self):
        try:
            import pystray
            from PIL import ImageDraw
            img = Image.new("RGB", (64, 64), "#1e6fd9")
            d = ImageDraw.Draw(img)
            d.rectangle((10, 14, 54, 42), outline="white", width=4)
            d.rectangle((26, 44, 38, 50), fill="white")
            d.rectangle((18, 50, 46, 54), fill="white")
            menu = pystray.Menu(
                pystray.MenuItem("Open", lambda: self.root.after(0, self.show), default=True),
                pystray.MenuItem("Disconnect viewer", lambda: self.svc.disconnect_viewer()),
                pystray.MenuItem("Quit", lambda: self.root.after(0, self._quit)))
            icon = pystray.Icon(APP, img, f"{APP} Host", menu)
            icon.run_detached()
            return icon
        except Exception:
            return None

    def show(self):
        self.root.deiconify()
        self.root.lift()

    def _refresh_network(self):
        def probe():
            addresses = local_ips()
            def apply():
                port = self.svc.cfg.get("listen") or DEFAULT_DIRECT_PORT
                self.addr = f"{addresses[0]}:{port}" if addresses else ""
                self.addr_var.set(self.addr or "(no network)")
                note, color = status_text(addresses)
                self.network_note.configure(text=note, foreground=color)
                self._show_service_state()
                self.root.after(15000, self._refresh_network)
            try:
                self.root.after(0, apply)
            except RuntimeError:
                pass
        threading.Thread(target=probe, daemon=True).start()

    def hide(self):
        self.root.withdraw()

    def _close_window(self):
        if self.tray:
            self.hide()
        else:
            self._quit()

    def _tick(self):
        blocked = self.svc.port_blocked
        if blocked != self._blocked_seen:
            self._blocked_seen = blocked
            self._shared_by_service = bool(blocked) and self._service_follows() and all(
                service_setup.is_service_agent(p) or not p for _, p in blocked)
            if blocked and not self._shared_by_service:
                self.free_btn.pack(side="right", padx=(0, 4))
            else:
                self.free_btn.pack_forget()
        if blocked:
            port = self.svc.cfg.get("listen") or DEFAULT_DIRECT_PORT
            self.svc.status = (f"Shared through the admin service on port {port} (same password)"
                               if self._shared_by_service else f"Port {port} is in use by {self._describe(blocked)}")
        self.status_var.set(self.svc.status)
        connected = self.svc.session is not None
        self.kick.state(["!disabled"] if connected else ["disabled"])
        self.root.title(f"{APP} - Share this PC" + ("  -  SOMEONE IS CONNECTED" if connected else ""))
        if self.tray:
            try:
                self.tray.title = f"{APP} Host - {self.svc.status}"[:127]
            except Exception:
                pass
        self.root.after(500, self._tick)

    def _copy(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def _new_pw(self):
        self.svc.set_password(new_password())
        self.pw_var.set(self.svc.cfg["password"])

    def _set_pw(self):
        from tkinter import messagebox, simpledialog
        pw = simpledialog.askstring(APP, "Type the password you want to use to connect\n"
                                         "(at least 8 characters, a long one is safer):", parent=self.root)
        if pw is None:
            return
        pw = pw.strip()
        if len(pw) < 8:
            messagebox.showwarning(APP, "That password is too short. Use at least 8 characters.", parent=self.root)
            return
        self.svc.set_password(pw)
        self.pw_var.set(pw)

    def _toggle_adv(self):
        self.adv_open = not self.adv_open
        if self.adv_open:
            self.adv.pack(after=self.adv_btn, fill="x", pady=(6, 0))
        else:
            self.adv.pack_forget()

    def _save_net(self):
        cfg = self.svc.cfg
        cfg["relay"] = self.relay_var.get().strip()
        cfg["relay_key"] = self.rkey_var.get().strip()
        try:
            cfg["listen"] = int(self.listen_var.get() or 0)
        except ValueError:
            cfg["listen"] = 0
        save_json("host.json", cfg)
        updater.set_sources(self.usrc_var.get())
        self.svc.restart_network()

    def _save_input(self):
        self.svc.cfg["allow_input"] = self.input_var.get()
        if self.svc.session:
            self.svc.session.injector.enabled = self.svc.cfg["allow_input"]
            if not self.svc.cfg["allow_input"]:
                self.svc.session.injector.release_all()
        save_json("host.json", self.svc.cfg)

    @staticmethod
    def _describe(owners):
        names = []
        for pid, path in owners:
            if service_setup.is_service_agent(path) or (not path and service_setup.state() == "running"):
                names.append("the AnyKir admin service")
            else:
                names.append(f"{os.path.basename(path) or 'another program'} (PID {pid})")
        return " and ".join(dict.fromkeys(names))

    def _free_port(self):
        from tkinter import messagebox
        port = self.svc.cfg.get("listen") or DEFAULT_DIRECT_PORT
        owners = port_owners(port)
        if owners:
            text = (f"Port {port} is in use by {self._describe(owners)}, so viewers may reach it instead of "
                    f"this window (and get \"wrong password\").\n\nClose it so this window gets the port?")
            if any(service_setup.is_service_agent(p) or not p for _, p in owners) and service_setup.state():
                text += ("\n\nThe admin service is stopped until Windows restarts. To stop it for good, "
                         "remove it under Advanced settings.")
            if not messagebox.askyesno(APP, text, parent=self.root):
                return
        self.free_btn.state(["disabled"])

        def work():
            need_admin = any(service_setup.is_service_agent(p) or not p or not kill_process(pid) for pid, p in owners)
            if need_admin:
                service_setup.run_elevated("free-port", port)
            try:
                self.root.after(0, done)
            except RuntimeError:
                pass

        def done():
            self.free_btn.state(["!disabled"])
            self.svc._service_seen = 0.0
            self.svc.restart_network()
            self._show_service_state()
        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _service_follows():
        return service_setup.state() == "running" and service_setup.installed_version() is not None

    def _show_service_state(self):
        st = service_setup.state()
        ver = service_setup.installed_version() if st else None
        if st == "running" and ver is None:
            self.svc_note.configure(text="The admin service is an old version with its own password. "
                                         "Click Reinstall under Advanced settings once; after that it "
                                         "follows this window and updates itself.")
            self.svc_note.pack(anchor="w", pady=(8, 0), after=self.network_note)
        else:
            self.svc_note.pack_forget()
        self._blocked_seen = object()
        if self.svc_btn is None:
            return
        if st and ver:
            state_text = f"Admin/UAC service v{ver}: {'running' if st == 'running' else 'stopped'}"
            if updater.newer(VERSION, ver):
                state_text += f" (updating to v{VERSION})"
        else:
            state_text = {"running": "Admin/UAC service: running (old version)",
                          "stopped": "Admin/UAC service: installed, stopped"}.get(st, "Admin/UAC service: not installed")
        self.svc_label.configure(text=state_text)
        self.svc_btn.configure(text="Remove" if st else "Install admin service")
        self.svc_btn.state(["!disabled"])
        if st and service_setup.available():
            self.svc_reinstall.pack(side="right", padx=(0, 4))
            self.svc_reinstall.state(["!disabled"])
        else:
            self.svc_reinstall.pack_forget()

    def _toggle_service(self, reinstall=False):
        from tkinter import messagebox
        installed = service_setup.state() is not None
        if self.svc.session:
            messagebox.showwarning(APP, "Disconnect the viewer first.", parent=self.root)
            return
        if installed and not reinstall:
            question = "Remove the administrator service?\n\nWindows will ask for administrator permission."
        else:
            question = ("Reinstall the administrator service?\n\nIt is replaced with this version and takes this "
                        "window's address and password." if installed else
                        "Install the administrator service?\n\n"
                        "It lets you control administrator windows and UAC prompts, and keeps this PC reachable "
                        "at the Windows sign-in screen.") + (
                        "\n\nWindows will ask for administrator permission. Afterwards the service shares this PC "
                        "with the same address and password and this window closes. File transfer and clipboard "
                        "are not available through the service.")
        if not messagebox.askyesno(APP, question, parent=self.root):
            return
        self.svc_btn.state(["disabled"])
        self.svc_reinstall.state(["disabled"])
        installing = reinstall or not installed
        if installing:
            self.svc.stop()

        def work():
            code = service_setup.run_elevated("install" if installing else "uninstall")
            try:
                self.root.after(0, lambda: self._service_done(installing and code == 0))
            except RuntimeError:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _service_done(self, installed_ok):
        if installed_ok and service_setup.state() == "running":
            if get_autostart():
                set_autostart(False)
            self._quit_for_update()
            return
        self.svc.stopping = False
        self.svc.restart_network()
        self._show_service_state()

    def _quit_for_update(self):
        self.svc.stop()
        if self.tray:
            self.tray.stop()
        self.root.destroy()

    def _quit(self):
        from tkinter import messagebox
        if self.svc.session and not messagebox.askyesno(APP, "A viewer is connected. Stop hosting?"):
            return
        self.svc.stop()
        if self.tray:
            self.tray.stop()
        self.root.destroy()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--nogui", action="store_true")
    ap.add_argument("--service-agent", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--relay", help="relay host:port (saved to config)")
    ap.add_argument("--relay-key", help="relay shared key (saved to config)")
    ap.add_argument("--listen", type=int, help="also accept direct connections on this port (saved)")
    ap.add_argument("--set-password", help="set a specific password (use a long one!)")
    ap.add_argument("--minimized", action="store_true", help="start hidden in the system tray")
    args = ap.parse_args(argv)
    if args.service_agent and not windows_desktop.enabled:
        raise PermissionError("Service agents must be launched by the AnyKir Windows service.")

    cfg = load_config()
    if args.service_agent:
        cfg["allow_update"] = False
        cfg["allow_files"] = False
        cfg["allow_clipboard"] = False
    if args.relay is not None:
        cfg["relay"] = args.relay
    if args.relay_key is not None:
        cfg["relay_key"] = args.relay_key
    if args.listen is not None:
        cfg["listen"] = args.listen
    if args.set_password:
        cfg["password"] = args.set_password
    save_json("host.json", cfg)

    service = HostService(cfg)
    service.start()
    if args.service_agent:
        import windows_service
        windows_service.follow_app(service)

    if args.nogui:
        print(f"{APP} host  ID: {cfg['id']}   password: {cfg['password']}", flush=True)
        try:
            while True:
                time.sleep(3)
        except KeyboardInterrupt:
            service.stop()
        return

    import tkinter as tk
    root = tk.Tk()
    app = HostApp(root, service)
    if args.minimized and app.tray:
        app.hide()
    root.mainloop()


if __name__ == "__main__":
    try:
        if "--nogui" in sys.argv or acquire_single_instance():
            main()
    except Exception:
        import traceback
        with open(os.path.join(config_dir(), "host-crash.log"), "a", encoding="utf-8") as _f:
            _f.write(time.strftime("%Y-%m-%d %H:%M:%S\n") + traceback.format_exc() + "\n")
        raise
