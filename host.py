__import__("os").environ.setdefault("OPENBLAS_NUM_THREADS", "1")
import argparse
import ctypes
import io
from common import clean_allow_list, ip_allowed
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

import hub
import service_setup
import updater
import windows_desktop
from desktop_capture import DesktopCapture
from network_info import is_tailscale, kill_process, local_ips, port_owners, set_tailscale, status_text, tailscale_state
from common import (APP, CHUNK, VERSION, acquire_single_instance, config_dir, show_hook, unlock, DEFAULT_DIRECT_PORT, T_CHUNK, T_FRAME, T_JSON,
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

import mss
import numpy as np
import pyperclip
from PIL import Image
from pynput import keyboard, mouse

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
try:
    import mss.windows.gdi as _mss_gdi
    _mss_gdi.CAPTUREBLT = 0
except Exception:
    pass

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


def _prewarm(delay=4.0):
    time.sleep(delay)
    try:
        if sys.platform == "win32" and not windows_desktop.enabled:
            import dxcam
        Image.new("RGB", (16, 16)).save(io.BytesIO(), "JPEG", quality=65)
    except Exception:
        pass


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


class _GuiThreadInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("flags", ctypes.c_uint32), ("hwndActive", ctypes.c_void_p),
                ("hwndFocus", ctypes.c_void_p), ("hwndCapture", ctypes.c_void_p), ("hwndMenuOwner", ctypes.c_void_p),
                ("hwndMoveSize", ctypes.c_void_p), ("hwndCaret", ctypes.c_void_p), ("rcCaret", ctypes.c_long * 4)]


def text_caret_active():
    if sys.platform != "win32":
        return False
    u = ctypes.windll.user32
    u.GetGUIThreadInfo.argtypes = [ctypes.c_uint32, ctypes.POINTER(_GuiThreadInfo)]
    info = _GuiThreadInfo(cbSize=ctypes.sizeof(_GuiThreadInfo))
    if not u.GetGUIThreadInfo(0, ctypes.byref(info)):
        return False
    return bool(info.hwndCaret)


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
        self.frame, self.frame_cv, self.frame_time = None, threading.Condition(), 0.0
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
        threading.Thread(target=self._caret_loop, daemon=True).start()
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

    def _ctrl_alt_del(self):
        if not self.injector.enabled or sys.platform != "win32":
            return
        if not windows_desktop.enabled:
            self.ch.send_json({"t": "notice", "text": "Ctrl+Alt+Del and unlocking need the admin service on that PC "
                                                      "(Share this PC > Advanced settings > Install admin service)."})
            return
        try:
            import windows_service
            windows_service.request_sas()
        except Exception as exc:
            self.ch.send_json({"t": "notice", "text": f"Ctrl+Alt+Del failed: {exc}"})

    def _handle(self, m):
        t = m.get("t")
        if t == "combo" and sorted(m.get("keys", [])) == ["alt", "ctrl", "delete"]:
            t = "cad"
        if t in ("mouse", "wheel", "key", "combo", "lock", "cad"):
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
        elif t == "cad":
            self._ctrl_alt_del()
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
        if self.last_clip is None:
            try:
                self.last_clip = pyperclip.paste()
            except Exception:
                self.last_clip = ""
        while not self.stop.wait(0.8):
            if not self.service.cfg.get("allow_clipboard", True):
                continue
            if not windows_desktop.input_is_default():
                continue
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

    def _caret_loop(self):
        if sys.platform != "win32":
            return
        desktop = windows_desktop.DesktopBinding()
        state = {"probe": None}

        def drop_probe():
            if state["probe"]:
                state["probe"].close()
                state["probe"] = None
        last = None
        try:
            while not self.stop.wait(0.25):
                try:
                    desktop.sync(before_switch=drop_probe)
                except OSError:
                    continue
                if state["probe"] is None:
                    try:
                        from text_focus import FocusProbe
                        state["probe"] = FocusProbe()
                    except Exception:
                        state["probe"] = False
                probe = state["probe"]
                try:
                    caret = text_caret_active() or bool(probe and probe.editable())
                    if caret != last:
                        last = caret
                        self.ch.send_json({"t": "caret", "on": caret})
                except OSError:
                    break
                except Exception:
                    pass
        finally:
            drop_probe() if state["probe"] else None
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
                with self.frame_cv:
                    waiting = self.frame is not None and t0 - self.frame_time < 0.1
                if waiting and not self.force_full.is_set():
                    self.stop.wait(0.004)
                    continue
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
                        self.frame_time = time.time()
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
                    fw, fh = (max(1, int(w * scale)), max(1, int(h * scale))) if scale < 1.0 else (w, h)

                    key = (idx, scale, fw, fh)
                    if key != prev_key:
                        self.injector.geom = (mon["left"], mon["top"], mon["width"], mon["height"])
                        self.ch.send_json({"t": "screen", "w": fw, "h": fh, "count": count, "mon": idx, "cap": cap})
                        prev, prev_key = None, key
                    if self.force_full.is_set():
                        self.force_full.clear()
                        prev = None

                    changed = [(0, 0, w, h)] if prev is None else changed_rects(raw, prev)
                    if not changed:
                        continue
                    quality = int(min(max(cfg["quality"], 10), 95))
                    sent = 0
                    src = Image.frombuffer("RGB", (w, h), raw, "raw", "BGRX", 0, 1) if scale < 1.0 else None
                    for x0, y0, x1, y1 in changed:
                        if src is None:
                            region = np.ascontiguousarray(raw[y0:y1, x0:x1])
                            im = Image.frombuffer("RGB", (x1 - x0, y1 - y0), region, "raw", "BGRX", 0, 1)
                        else:
                            sx0, sy0 = x0 * fw // w, y0 * fh // h
                            sx1, sy1 = min(fw, -(-x1 * fw // w)), min(fh, -(-y1 * fh // h))
                            if sx1 <= sx0 or sy1 <= sy0:
                                continue
                            im = src.resize((sx1 - sx0, sy1 - sy0), Image.BILINEAR,
                                            box=(sx0 * w / fw, sy0 * h / fh, sx1 * w / fw, sy1 * h / fh))
                            x0, y0 = sx0, sy0
                        buf = io.BytesIO()
                        im.save(buf, "JPEG", quality=quality)
                        self.ch.send(T_FRAME, struct.pack(">HHHH", x0, y0, im.width, im.height) + buf.getvalue())
                        sent += 1
                    prev = raw
                    with self.flow:
                        self.sent += sent
                    self.active_until = max(self.active_until, time.time() + 1.0)
                except OSError:
                    return
                except Exception:
                    prev = None
                    time.sleep(0.2)
                    continue
                work = time.time() - t0
                delay = max(1.0 / max(1, min(int(cfg["fps"]), 60)) - work, work)
                if delay > 0:
                    time.sleep(delay)
        finally:
            self.stop.set()


_watch = {"svc": None, "started": False}


class HostService:

    def __init__(self, cfg):
        self.cfg = cfg
        self.key = derive_key(cfg["password"], cfg["id"])
        self.status = "Starting..."
        self.session = None
        self.fails = []
        self.stopping = False
        self._lock = threading.Lock()
        self._listener = None
        self._gen = 0
        self.port_blocked = None
        self._service_seen = 0.0
        self._hub_lines = []
        self.on_stop = None

    def set_password(self, password):
        self.cfg["password"] = password
        self.key = derive_key(password, self.cfg["id"])
        save_json("host.json", self.cfg)

    def start(self):
        self.restart_network()
        threading.Thread(target=_prewarm, daemon=True).start()
        updater.ensure_server()
        if not windows_desktop.enabled:
            _watch["svc"] = self
            if not _watch["started"]:
                _watch["started"] = True
                updater.Watcher(lambda: _watch["svc"] and _watch["svc"]._update_ready()).start()

    def _update_ready(self):
        if not windows_desktop.enabled and not self.session and not self.stopping:
            updater.restart_now()

    def bind_ip(self):
        ip = self.cfg.get("address")
        return ip if self.cfg.get("only_address") and ip else ""

    def shared_ips(self):
        ip = self.bind_ip()
        return [ip] if ip else local_ips()

    def restart_network(self):
        self._gen += 1
        if self._listener:
            try:
                self._listener.close()
            except OSError:
                pass
        self._listener = None
        self.status = "Ready - waiting for connection"
        threading.Thread(target=self._listen_loop, args=(self._gen,), daemon=True).start()
        self._start_hub_lines(self._gen)

    def _start_hub_lines(self, gen):
        for line in self._hub_lines:
            if line["sock"]:
                try:
                    line["sock"].close()
                except OSError:
                    pass
        hello = lambda: {"id": self.cfg["id"], "name": socket.gethostname(), "ver": VERSION,
                         "port": int(self.cfg.get("listen") or DEFAULT_DIRECT_PORT),
                         "admin": windows_desktop.enabled, "svc": service_info()}
        self._hub_lines = []
        for address in hub.clean_hubs(self.cfg.get("hubs", hub.DEFAULT_HUBS)):
            self._hub_lines.append(hub.call_home(
                address, hello, self.serve, lambda: not self.stopping and gen == self._gen, self.shared_ips,
                active=lambda: not self.yield_to_service(), on_stop=self._remote_stop, source=self.bind_ip))

    def _remote_stop(self):
        self.status = "Stopped by AnyKir Server"
        if windows_desktop.enabled:
            import windows_service
            windows_service.stop_everything()
        elif self.on_stop:
            self.on_stop()
        else:
            self.stop()

    def disconnect_viewer(self):
        s = self.session
        if s:
            s.close()

    def stop(self):
        self.stopping = True
        self._gen += 1
        self.disconnect_viewer()
        for line in self._hub_lines:
            if line["sock"]:
                try:
                    line["sock"].close()
                except OSError:
                    pass
        if self._listener:
            try:
                self._listener.close()
            except OSError:
                pass

    SERVICE_GRACE = 90

    def yield_to_service(self):
        if windows_desktop.enabled or sys.platform != "win32":
            return False
        now = time.time()
        if service_setup.state() == "running" and service_setup.follows_this_app():
            self._service_seen = now
            return True
        return now - self._service_seen < self.SERVICE_GRACE

    def _listen_loop(self, gen):
        port = int(self.cfg.get("listen") or DEFAULT_DIRECT_PORT)
        while not self.stopping and gen == self._gen:
            if self.yield_to_service():
                self.port_blocked = None
                self.status = f"Shared through the admin service on port {port} (same password)"
                time.sleep(5)
                continue
            srv = socket.socket()
            try:
                if sys.platform == "win32":
                    srv.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", -5), 1)
                else:
                    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.bind((self.bind_ip() or "0.0.0.0", port))
                srv.listen(5)
            except OSError:
                srv.close()
                ip = self.bind_ip()
                if ip and ip not in local_ips():
                    self.port_blocked = None
                    self.status = f"Waiting for {ip} - that address is not on this PC right now"
                    time.sleep(5)
                    continue
                self.port_blocked = port_owners(port)
                self.status = f"Port {port} is in use by another program - retrying"
                time.sleep(5)
                continue
            if gen != self._gen or self.stopping:
                srv.close()
                return
            self._listener = srv
            if self.port_blocked is not None or self.status.startswith("Shared"):
                self.port_blocked = None
                self.status = "Ready - waiting for connection"
            srv.settimeout(5)
            try:
                while not self.stopping and gen == self._gen:
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

    def serve(self, sock, label, ip=None):
        ip = label if ip is None else ip
        if not ip_allowed(self.cfg, ip):
            print(f"[{time.strftime('%H:%M:%S')}] refused {ip or 'an unknown address'} via {label}: "
                  "not on the allowed list", flush=True)
            sock.close()
            return
        with self._lock:
            if self._locked_out():
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
            old = self.session
        if old:
            old.close()
            for _ in range(50):
                if self.session is not old:
                    break
                time.sleep(0.1)
        with self._lock:
            if self.session:
                ch.close()
                return
            session = HostSession(ch, self, label)
            self.session = session
        self.status = f"Connected: viewer {ip} via {label}" if ip and ip != label else f"Connected: viewer via {label}"
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
        "listen": DEFAULT_DIRECT_PORT, "hubs": list(hub.DEFAULT_HUBS), "allow_input": True, "allow_clipboard": True, "allow_update": True, "autostart": True,
    }
    for k, v in defaults.items():
        if k not in cfg:
            cfg[k] = v
            changed = True
    for old in ("relay", "relay_key"):
        if cfg.pop(old, None) is not None:
            changed = True
    if not cfg.get("listen"):
        cfg["listen"] = DEFAULT_DIRECT_PORT
        changed = True
    if changed:
        save_json("host.json", cfg)
    return cfg


def service_info():
    if windows_desktop.enabled:
        return {"ver": VERSION, "state": "running"}
    try:
        import windows_service
        ver, st = windows_service.installed_version(), service_setup.state()
    except Exception:
        return {}
    return {"ver": ver or "", "state": st} if st else {}


def autostart_command():
    if getattr(sys, "frozen", False):
        import fastcopy
        return f'"{fastcopy.launcher()}" --minimized'
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
    return autostart_value() is not None


def autostart_value():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            return winreg.QueryValueEx(k, APP + "Host")[0]
    except Exception:
        return None


def refresh_autostart():
    value = autostart_value()
    if value is not None and value != autostart_command():
        try:
            set_autostart(True)
        except OSError:
            pass


class HostApp:
    def __init__(self, root, service):
        import tkinter as tk
        from tkinter import ttk
        import ui
        self.tk, self.ttk, self.ui, self.root, self.svc = tk, ttk, ui, root, service
        self.back_to_menu = False
        service.on_stop = lambda: root.after(0, self._quit_for_update)
        cfg = service.cfg
        self._keep = []
        root.title(f"{APP} - Share this PC")
        ui.window_size(root, 580, 900)
        ui.appbar(root, VERSION, back=self._main_menu)
        f = ui.scroll_area(root)

        addresses = local_ips(tailscale=False)
        self.addr = self._pick_address(addresses)
        self.banner = ui.card(f, style="Ok.TFrame", padding=(16, 12))
        texts = ttk.Frame(self.banner, style="OkPlain.TFrame")
        texts.pack(side="left", fill="x", expand=True)
        actions = ttk.Frame(self.banner, style="OkPlain.TFrame")
        actions.pack(side="right", before=texts)
        self.status_var = tk.StringVar()
        self.status_lbl = ui.auto_wrap(ttk.Label(texts, textvariable=self.status_var, style="Ok.Title.TLabel"))
        self.status_lbl.pack(anchor="w", fill="x")
        note, _color = status_text(addresses)
        self.network_note = ui.auto_wrap(ttk.Label(texts, text=note, style="Ok.Muted.TLabel"))
        self.network_note.pack(anchor="w", fill="x")
        self._banner_texts = texts
        self._banner_actions = actions
        self.kick = ttk.Button(actions, text="Disconnect", style="OkDanger.TButton", command=service.disconnect_viewer)
        self.free_btn = ttk.Button(actions, text="Free port", style="OkTonal.TButton", command=self._free_port)
        self._banner_kind = "Ok"
        self.svc_note_card = ui.card(f, style="Warn.TFrame", padding=(16, 12))
        self.svc_note = ui.auto_wrap(ttk.Label(self.svc_note_card, style="Warn.TLabel"))
        self.svc_note.pack(anchor="w", fill="x")
        self.svc_note_card.pack_forget()
        self._blocked_seen = None
        self._shared_by_service = False

        self.headline = ui.headline(f, "Share this PC")
        card = ui.card(f)
        ttk.Label(card, text="Address", style="Card.Label.TLabel").pack(anchor="w")
        r = ttk.Frame(card, style="Plain.TFrame")
        r.pack(fill="x")
        self.addr_var = tk.StringVar(value=self.addr or "(no network)")
        self.addr_cb = ttk.Combobox(r, textvariable=self.addr_var, state="readonly", width=21,
                                    style="Address.TCombobox", font=(ui.MONO, 17, "bold"))
        self.addr_cb.pack(side="left")
        self.addr_cb.bind("<<ComboboxSelected>>", self._choose_address)
        ttk.Button(r, text="Copy", style="CardTonal.TButton", command=lambda: self._copy(self.addr)).pack(
            side="right", before=self.addr_cb)
        self.also = ui.auto_wrap(ttk.Label(card, style="Card.Small.TLabel"))
        self.also.pack(anchor="w", fill="x")
        r = ttk.Frame(card, style="Plain.TFrame")
        r.pack(fill="x", pady=(8, 0))
        ttk.Label(r, text="Share on", style="Card.Small.TLabel").pack(side="left", padx=(0, 8))
        self._only_buttons = {}
        for key, text in ((False, "All addresses"), (True, "Only this address")):
            b = ttk.Button(r, text=text, command=lambda k=key: self._set_only_address(k))
            b.pack(side="left", padx=(0, 6))
            self._only_buttons[key] = b
        self._fill_addresses(addresses)
        ui.divider(card, pady=10)
        ttk.Label(card, text="Password", style="Card.Label.TLabel").pack(anchor="w")
        r = ttk.Frame(card, style="Plain.TFrame")
        r.pack(fill="x")
        self.pw_var = tk.StringVar()
        pw_lbl = ttk.Label(r, textvariable=self.pw_var, style="Card.Big.TLabel")
        pw_lbl.pack(side="left")
        ttk.Button(r, text="Copy", style="CardTonal.TButton",
                   command=lambda: self._copy(self.svc.cfg["password"])).pack(side="right", before=pw_lbl)
        self.addr_cb.configure(font=(ui.MONO, 17, "bold"))
        ui.fit_font(card, [self.addr_cb], (ui.MONO, 17, "bold"), (ui.MONO, 12, "bold"))
        ui.fit_font(card, [pw_lbl], (ui.MONO, 17, "bold"), (ui.MONO, 12, "bold"))
        self._buttons = {}
        for text, cmd in (("Change", self._set_pw), ("Show", self._toggle_pw)):
            b = ttk.Button(r, text=text, style="CardText.TButton", command=cmd)
            b.pack(side="right", padx=(0, 2), before=pw_lbl)
            self._buttons[text] = b
        self._pw_visible = False
        self._show_pw()

        settings = self.settings = ui.card(f, padding=(16, 4))
        self.ts_var = tk.BooleanVar(value=False)
        self.ts_row = ui.setting_row(settings, "Tailscale", "Reach this PC from anywhere", self.ts_var,
                                     command=self._toggle_tailscale)
        self.ts_btn = self.ts_row.switch
        self._ts_state = None
        first_row = None
        if sys.platform == "win32":
            want = bool(cfg.get("autostart", True))
            if want != get_autostart() or (want and autostart_value() != autostart_command()):
                try:
                    set_autostart(want)
                except OSError:
                    pass
            self.auto_var = tk.BooleanVar(value=want)
            first_row = ui.setting_row(settings, "Start with Windows", "Share this PC as soon as you sign in",
                                       self.auto_var, command=self._save_autostart)
        self.input_var = tk.BooleanVar(value=cfg["allow_input"])
        self.svc_btn = None
        has_service = service_setup.available() or service_setup.state() is not None
        ui.setting_row(settings, "Allow remote control", "Off = the other side can only watch", self.input_var,
                       command=self._save_input)
        row = self._who_row(settings, last=not has_service)
        self._after_ts = first_row or row
        if has_service:
            row = ui.setting_row(settings, "Admin service", "", last=True)
            self.svc_label = row.sub
            row.sub.pack(anchor="w", fill="x")
            self.svc_btn = ttk.Button(row.right, style="CardText.TButton", command=self._toggle_service)
            self.svc_btn.pack(side="right")
            self.svc_reinstall = ttk.Button(row.right, text="Reinstall", style="CardText.TButton",
                                            command=lambda: self._toggle_service(reinstall=True))
        self._show_tailscale(None)
        threading.Thread(target=lambda: self._later(self._show_tailscale, tailscale_state()), daemon=True).start()
        root.after(300, self._refresh_network)

        self.adv = ui.expander(f, "Advanced settings")
        self.adv.head.pack_configure(fill="none", anchor="w", pady=(2, 0))
        self.adv_btn = self.adv.head
        self.listen_var = tk.StringVar(value=str(cfg["listen"] or DEFAULT_DIRECT_PORT))
        self.usrc_var = tk.StringVar(value=", ".join(updater.user_sources() or updater.DEFAULT_SOURCES))
        self.hubs_var = tk.StringVar(value=", ".join(hub.clean_hubs(cfg.get("hubs", hub.DEFAULT_HUBS))))
        for label, var in (("Port", self.listen_var), ("AnyKir Server", self.hubs_var),
                           ("Update source", self.usrc_var)):
            ui.field(self.adv, label, var, on_enter=self._save_net)
        r = ttk.Frame(self.adv)
        r.pack(fill="x", pady=(8, 0))
        ttk.Button(r, text="Save and reconnect", style="Tonal.TButton", command=self._save_net).pack(side="right")
        if sys.platform == "win32":
            self.fw_btn = ttk.Button(r, text="Allow through firewall", style="Text.TButton",
                                     command=lambda: self._allow_firewall(True))
            self.fw_btn.pack(side="left")
            root.after(1500, self._check_firewall)
        ttk.Label(self.adv, text="Appearance", style="Label.TLabel").pack(anchor="w", pady=(14, 6))
        ui.appearance(self.adv).pack(anchor="w")
        self._show_service_state()

        self.tray = self._make_tray()
        show_hook["fn"] = lambda: root.after(0, self.show)
        updater.quit_hook["fn"] = lambda: root.after(0, self._quit_for_update)
        root.protocol("WM_DELETE_WINDOW", self._close_window)
        self._tick()

    def _later(self, fn, *args):
        try:
            self.root.after(0, lambda: fn(*args))
        except RuntimeError:
            pass

    def _make_tray(self):
        try:
            import pystray
            import logo
            img = logo.image(64)
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
        if self.root.state() == "withdrawn" and not unlock(self.root, "open"):
            return
        self.root.deiconify()
        self.root.lift()

    def _save_autostart(self):
        self.svc.cfg["autostart"] = self.auto_var.get()
        save_json("host.json", self.svc.cfg)
        set_autostart(self.auto_var.get())

    def _refresh_network(self):
        def probe():
            addresses = local_ips()
            ts = tailscale_state()
            def apply():
                self.addr = self._pick_address(addresses)
                self.addr_var.set(self.addr or "(no network)")
                self._fill_addresses(addresses)
                self._show_tailscale(ts)
                note, color = status_text(addresses)
                self.network_note.configure(text=note)
                self._show_service_state()
                self.root.after(15000, self._refresh_network)
            try:
                self.root.after(0, apply)
            except RuntimeError:
                pass
        threading.Thread(target=probe, daemon=True).start()

    def _pick_address(self, addresses):
        port = self.svc.cfg.get("listen") or DEFAULT_DIRECT_PORT
        chosen = self.svc.cfg.get("address")
        ip = chosen if chosen in addresses else (addresses[0] if addresses else "")
        return f"{ip}:{port}" if ip else ""

    def _fill_addresses(self, addresses):
        self._addresses = list(addresses)
        only = bool(self.svc.cfg.get("only_address"))
        for key, b in getattr(self, "_only_buttons", {}).items():
            b.configure(style="CardSegOn.TButton" if key == only else "CardSeg.TButton")
        port = self.svc.cfg.get("listen") or DEFAULT_DIRECT_PORT
        self.addr_cb["values"] = [f"{ip}:{port}" + ("  (Tailscale)" if is_tailscale(ip) else "") for ip in addresses]
        self.addr_cb.configure(width=max(len(self.addr or "(no network)"), 12))
        others = [f"{ip}:{port}" + (" (Tailscale)" if is_tailscale(ip) else "") for ip in addresses
                  if f"{ip}:{port}" != self.addr]
        if only:
            self.also.configure(text="Only this address accepts viewers" +
                                (f" (not used: {len(others)} other)" if len(others) == 1 else
                                 f" (not used: {len(others)} others)" if others else ""))
        else:
            self.also.configure(text=("Also: " + ",  ".join(others[:3])) if others else "")

    def _choose_address(self, _e=None):
        value = self.addr_var.get().split()[0]
        before = self.svc.bind_ip()
        self.svc.cfg["address"] = value.rsplit(":", 1)[0]
        save_json("host.json", self.svc.cfg)
        self.addr = value
        self.addr_var.set(value)
        self._fill_addresses(getattr(self, "_addresses", []))
        if self.svc.bind_ip() != before:
            self.svc.restart_network()

    def _set_only_address(self, only):
        cfg = self.svc.cfg
        if only:
            ip = (self.addr or "").rsplit(":", 1)[0]
            if not ip:
                return
            cfg["address"] = ip
        if bool(cfg.get("only_address")) == only and not only:
            return
        before = self.svc.bind_ip()
        cfg["only_address"] = only
        save_json("host.json", cfg)
        self._fill_addresses(getattr(self, "_addresses", []))
        if self.svc.bind_ip() != before:
            self.svc.restart_network()

    def _show_tailscale(self, state):
        self._ts_state = state
        if state is None:
            self.ts_row.pack_forget()
            return
        self.ts_var.set(state == "on")
        self.ts_btn.state(["!disabled"])
        self.ts_row.sub.configure(text="On - reach this PC from anywhere" if state == "on"
                                  else "Off - only this network can reach this PC")
        if not self.ts_row.winfo_ismapped():
            self.ts_row.pack(fill="x", before=self._after_ts)

    def _toggle_tailscale(self):
        from tkinter import messagebox
        turn_on = self._ts_state != "on"
        if not turn_on:
            text = ("Turn Tailscale off on this PC?\n\n"
                    "Viewers connecting through a Tailscale (100.x) address lose access.")
            if self.svc.session:
                text += "\n\nA viewer is connected now and may be disconnected."
            if not messagebox.askyesno(APP, text, parent=self.root):
                self.ts_var.set(True)
                return
        self.ts_btn.state(["disabled"])
        self.ts_row.sub.configure(text="Turning on..." if turn_on else "Turning off...")

        def work():
            ok, err = set_tailscale(turn_on)
            addresses, state = local_ips(), tailscale_state()

            def done():
                self._show_tailscale(state)
                self.addr = self._pick_address(addresses)
                self.addr_var.set(self.addr or "(no network)")
                self._fill_addresses(addresses)
                note, color = status_text(addresses)
                self.network_note.configure(text=note)
                if not ok:
                    messagebox.showerror(APP, "Tailscale: " + err, parent=self.root)
            try:
                self.root.after(0, done)
            except RuntimeError:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _check_firewall(self):
        import windows_service
        port = self.svc.cfg.get("listen") or DEFAULT_DIRECT_PORT

        def probe():
            ready = windows_service.firewall_ready(port)
            if not ready:
                try:
                    self.root.after(0, lambda: self._allow_firewall(False))
                except RuntimeError:
                    pass
        threading.Thread(target=probe, daemon=True).start()

    def _allow_firewall(self, asked):
        from tkinter import messagebox
        port = self.svc.cfg.get("listen") or DEFAULT_DIRECT_PORT
        if not asked:
            if self.root.state() == "withdrawn" or getattr(self, "_fw_asked", False):
                return
            self._fw_asked = True
            if not messagebox.askyesno(APP, f"Windows Firewall is not set to let other PCs reach AnyKir on port {port}."
                                            "\n\nAllow it now? Windows will ask for administrator permission.",
                                       parent=self.root):
                return
        self.fw_btn.state(["disabled"])

        def work():
            import windows_service
            service_setup.run_elevated("allow-firewall", port)
            ready = windows_service.firewall_ready(port)

            def done():
                self.fw_btn.state(["!disabled"])
                if ready:
                    messagebox.showinfo(APP, f"AnyKir is allowed through Windows Firewall (port {port}, 7002 and ping).",
                                        parent=self.root)
                else:
                    messagebox.showwarning(APP, "The firewall rule was not added.", parent=self.root)
            try:
                self.root.after(0, done)
            except RuntimeError:
                pass
        threading.Thread(target=work, daemon=True).start()

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
                self.free_btn.pack(side="right", padx=(6, 0))
            else:
                self.free_btn.pack_forget()
        if blocked:
            port = self.svc.cfg.get("listen") or DEFAULT_DIRECT_PORT
            self.svc.status = (f"Shared through the admin service on port {port} (same password)"
                               if self._shared_by_service else f"Port {port} is in use by {self._describe(blocked)}")
        self.status_var.set(self.svc.status)
        connected = self.svc.session is not None
        if connected and not self.kick.winfo_ismapped():
            self.kick.pack(side="right", padx=(6, 0))
        elif not connected and self.kick.winfo_ismapped():
            self.kick.pack_forget()
        status = self.svc.status
        trouble = (blocked and not self._shared_by_service) or status.startswith(("Stopped", "Port", "Starting"))
        self._paint_banner("Warn" if trouble else "Ok")
        self.root.title(f"{APP} - Share this PC" + ("  -  SOMEONE IS CONNECTED" if connected else ""))
        if self.tray:
            try:
                self.tray.title = f"{APP} Host - {self.svc.status}"[:127]
            except Exception:
                pass
        self.root.after(500, self._tick)

    def _paint_banner(self, kind):
        if kind == self._banner_kind:
            return
        self._banner_kind = kind
        self.banner.configure(style=f"{kind}.TFrame")
        for frame in (self._banner_texts, self._banner_actions):
            frame.configure(style=f"{kind}Plain.TFrame")
        self.status_lbl.configure(style=f"{kind}.Title.TLabel")
        self.network_note.configure(style=f"{kind}.Muted.TLabel")
        self.kick.configure(style=f"{kind}Danger.TButton")
        self.free_btn.configure(style=f"{kind}Tonal.TButton")

    def _copy(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def _show_pw(self):
        pw = self.svc.cfg["password"]
        self.pw_var.set(pw if self._pw_visible else "•" * min(len(pw), 12))
        self._buttons["Show"].configure(text="Hide" if self._pw_visible else "Show")

    def _toggle_pw(self):
        self._pw_visible = not self._pw_visible
        self._show_pw()

    def _new_pw(self):
        self.svc.set_password(new_password())
        self._show_pw()

    def _set_pw(self):
        tk, ttk, ui = self.tk, self.ttk, self.ui
        win = tk.Toplevel(self.root)
        win.title(APP)
        win.resizable(False, False)
        win.transient(self.root)
        ui.themed_window(win)
        f = ttk.Frame(win, padding=(22, 18))
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Change password", style="Title.TLabel").pack(anchor="w")
        ttk.Label(f, text="Use at least 8 characters. A long one is safer.", style="Muted.TLabel").pack(anchor="w",
                                                                                                    pady=(0, 8))
        var = tk.StringVar()
        entry = ui.field(f, "New password", var, show="\u2022", width=34)
        msg = ttk.Label(f, style="Error.TLabel")
        msg.pack(anchor="w")

        def random():
            var.set(new_password())
            entry.configure(show="")
            entry.icursor("end")

        def save(*_):
            pw = var.get().strip()
            if len(pw) < 8:
                msg.configure(text="That password is too short. Use at least 8 characters.")
                return
            self.svc.set_password(pw)
            self._show_pw()
            win.destroy()
        r = ttk.Frame(f)
        r.pack(fill="x", pady=(8, 0))
        ttk.Button(r, text="Random", style="Text.TButton", command=random).pack(side="left")
        ttk.Button(r, text="Save", style="Filled.TButton", command=save).pack(side="right")
        ttk.Button(r, text="Cancel", style="Text.TButton", command=win.destroy).pack(side="right", padx=(0, 6))
        entry.bind("<Return>", save)
        win.bind("<Escape>", lambda _e: win.destroy())
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_reqwidth()) // 2
        y = self.root.winfo_rooty() + 120
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        ui.reveal(win)
        entry.focus_force()
        win.grab_set()

    def _toggle_adv(self):
        self.adv.toggle()

    def _save_net(self):
        cfg = self.svc.cfg
        try:
            cfg["listen"] = int(self.listen_var.get()) if 0 < int(self.listen_var.get()) < 65536 else DEFAULT_DIRECT_PORT
        except ValueError:
            cfg["listen"] = DEFAULT_DIRECT_PORT
        self.listen_var.set(str(cfg["listen"]))
        save_json("host.json", cfg)
        updater.set_sources(self.usrc_var.get())
        cfg["hubs"] = hub.clean_hubs(self.hubs_var.get())
        self.hubs_var.set(", ".join(cfg["hubs"]))
        save_json("host.json", cfg)
        self.svc.restart_network()
        self.root.after(500, self._check_firewall)

    def _who_row(self, parent, last):
        ttk = self.ttk
        row = self.ui.setting_row(parent, "Who can connect", "", last=last)
        row.sub.pack(anchor="w", fill="x")
        self._who_label = row.sub
        self._who_buttons = {}
        for key, text in (("list", "Only these"), ("all", "Anyone")):
            b = ttk.Button(row.right, text=text, command=lambda k=key: self._pick_who(k))
            b.pack(side="right", padx=(6, 0))
            self._who_buttons[key] = b
        self._show_who()
        return row

    def _show_who(self):
        cfg = self.svc.cfg
        mode = "list" if cfg.get("allow_from_mode") == "list" else "all"
        for key, b in self._who_buttons.items():
            b.configure(style="CardSegOn.TButton" if key == mode else "CardSeg.TButton")
        listed = cfg.get("allow_from") or []
        if mode == "list":
            shown = ", ".join(listed[:4]) + (f" and {len(listed) - 4} more" if len(listed) > 4 else "")
            self._who_label.configure(text=f"Only {shown} (with the password) - click Only these to edit")
        else:
            self._who_label.configure(text="Anyone with the password")

    def _pick_who(self, mode):
        if mode == "list":
            self._edit_who()
            return
        self.svc.cfg["allow_from_mode"] = "all"
        save_json("host.json", self.svc.cfg)
        self._show_who()

    def _edit_who(self):
        tk, ttk, ui = self.tk, self.ttk, self.ui
        win = tk.Toplevel(self.root)
        win.title(APP)
        win.resizable(False, False)
        win.transient(self.root)
        ui.themed_window(win)
        f = ttk.Frame(win, padding=(22, 18))
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Only these addresses", style="Title.TLabel").pack(anchor="w")
        ttk.Label(f, text="Viewers from other addresses are refused before the password.\n"
                          "Separate with commas. 192.168.1.0/24 = every 192.168.1.x address.",
                  style="Muted.TLabel", justify="left").pack(anchor="w", pady=(0, 8))
        var = tk.StringVar(value=", ".join(self.svc.cfg.get("allow_from") or []))
        entry = ui.field(f, "Allowed addresses", var, width=44)
        msg = ttk.Label(f, style="Error.TLabel")
        msg.pack(anchor="w")

        def save(*_):
            good, bad = clean_allow_list(var.get())
            if bad:
                msg.configure(text="Not an address: " + ", ".join(bad[:3]))
                return
            if not good:
                msg.configure(text="Add at least one address (or choose Anyone).")
                return
            self.svc.cfg["allow_from"] = good
            self.svc.cfg["allow_from_mode"] = "list"
            save_json("host.json", self.svc.cfg)
            self._show_who()
            win.destroy()
        r = ttk.Frame(f)
        r.pack(fill="x", pady=(8, 0))
        ttk.Button(r, text="Save", style="Filled.TButton", command=save).pack(side="right")
        ttk.Button(r, text="Cancel", style="Text.TButton", command=win.destroy).pack(side="right", padx=(0, 6))
        entry.bind("<Return>", save)
        win.bind("<Escape>", lambda _e: win.destroy())
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_reqwidth()) // 2
        y = self.root.winfo_rooty() + 120
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        ui.reveal(win)
        entry.focus_force()
        entry.icursor("end")
        win.grab_set()

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
        return service_setup.state() == "running" and service_setup.follows_this_app()

    def _show_service_state(self):
        st = service_setup.state()
        ver = service_setup.installed_version() if st else None
        if st == "running" and ver is None:
            self.svc_note.configure(text="The admin service is an old version with its own password. "
                                         "Click Reinstall under Advanced settings once; after that it "
                                         "follows this window and updates itself.")
            if not self.svc_note_card.winfo_ismapped():
                self.svc_note_card.pack(fill="x", pady=(0, 10), after=self.banner)
        else:
            self.svc_note_card.pack_forget()
        self._blocked_seen = object()
        if self.svc_btn is None:
            return
        if st and ver:
            state_text = (f"v{ver} {'running' if st == 'running' else 'installed, stopped'} - works with admin "
                          "windows and the lock screen")
            if updater.newer(VERSION, ver):
                state_text += f" (updating to v{VERSION})"
        else:
            state_text = {"running": "Running (old version)", "stopped": "Installed, stopped"}.get(
                st, "Not installed - needed for admin windows, UAC prompts and the lock screen")
        self.svc_label.configure(text=state_text)
        self.svc_btn.configure(text="Remove" if st else "Install")
        self.svc_btn.state(["!disabled"])
        if st and service_setup.available():
            self.svc_reinstall.pack(side="right", padx=(0, 2))
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
                        "with the same address and password, and this window stays open. Copy and paste works "
                        "through the service; file transfer needs this window.")
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
        self.svc.stopping = False
        self.svc.restart_network()
        self._show_service_state()
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(300, lambda: self.root.attributes("-topmost", False))

    def _quit_for_update(self):
        self.svc.stop()
        if self.tray:
            self.tray.stop()
        self.root.destroy()

    def _main_menu(self):
        from tkinter import messagebox
        text = ("Stop sharing this PC from this window and go back to the main menu?"
                + ("\n\nA viewer is connected and will be disconnected." if self.svc.session else "")
                + ("\n\nThe admin service keeps sharing this PC." if service_setup.state() == "running" else ""))
        if not messagebox.askyesno(APP, text, parent=self.root):
            return
        self.back_to_menu = True
        self.svc.stop()
        if self.tray:
            self.tray.stop()
        self.root.destroy()

    def _quit(self):
        from tkinter import messagebox
        if not unlock(self.root, "quit"):
            return
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
    try:
        import logo
        logo.tk_icon(root)
    except Exception:
        pass
    import ui
    ui.apply(root)
    app = HostApp(root, service)
    if args.minimized and app.tray:
        app.hide()
    ui.reveal(root)
    root.mainloop()
    return app.back_to_menu


if __name__ == "__main__":
    try:
        if "--nogui" in sys.argv or acquire_single_instance():
            main()
    except Exception:
        import traceback
        with open(os.path.join(config_dir(), "host-crash.log"), "a", encoding="utf-8") as _f:
            _f.write(time.strftime("%Y-%m-%d %H:%M:%S\n") + traceback.format_exc() + "\n")
        raise
