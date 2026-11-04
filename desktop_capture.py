import ctypes
from ctypes import wintypes as w

import numpy as np
from PIL import ImageGrab


def monitors():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HANDLE, w.HDC, ctypes.POINTER(w.RECT), w.LPARAM)
    enum = user32.EnumDisplayMonitors
    enum.argtypes = [w.HDC, ctypes.POINTER(w.RECT), callback_type, w.LPARAM]
    enum.restype = w.BOOL
    rects = []

    @callback_type
    def collect(handle, dc, rect, data):
        r = rect.contents
        rects.append({"left": r.left, "top": r.top, "width": r.right - r.left, "height": r.bottom - r.top})
        return True

    if not enum(None, None, collect, 0) or not rects:
        raise OSError("Windows could not enumerate displays on the input desktop")
    rects.sort(key=lambda r: not (r["left"] <= 0 < r["left"] + r["width"] and
                                 r["top"] <= 0 < r["top"] + r["height"]))
    left, top = min(r["left"] for r in rects), min(r["top"] for r in rects)
    right = max(r["left"] + r["width"] for r in rects)
    bottom = max(r["top"] + r["height"] for r in rects)
    return [{"left": left, "top": top, "width": right - left, "height": bottom - top}] + rects


class DesktopCapture:
    name = "win32"

    def close(self):
        pass

    def grab(self, index):
        displays = monitors()
        index = min(max(int(index), 0), len(displays) - 1)
        image = ImageGrab.grab(all_screens=True, include_layered_windows=True)
        virtual, monitor = displays[0], displays[index]
        if image.size != (virtual["width"], virtual["height"]):
            raise OSError("Display geometry changed during capture; retrying")
        if index:
            x, y = monitor["left"] - virtual["left"], monitor["top"] - virtual["top"]
            image = image.crop((x, y, x + monitor["width"], y + monitor["height"]))
        image = image.convert("RGB")
        pixels = np.frombuffer(image.tobytes("raw", "BGRX"), dtype=np.uint32).reshape(image.height, image.width)
        return pixels, monitor, len(displays) - 1
