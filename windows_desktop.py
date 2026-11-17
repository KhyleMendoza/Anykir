import ctypes
import sys

enabled = False

DESKTOP_READOBJECTS = 0x0001
DESKTOP_CREATEWINDOW = 0x0002
DESKTOP_JOURNALPLAYBACK = 0x0020
DESKTOP_ENUMERATE = 0x0040
DESKTOP_WRITEOBJECTS = 0x0080
DESKTOP_SWITCHDESKTOP = 0x0100
DESKTOP_ACCESS = (DESKTOP_READOBJECTS | DESKTOP_CREATEWINDOW | DESKTOP_JOURNALPLAYBACK | DESKTOP_ENUMERATE
                  | DESKTOP_WRITEOBJECTS | DESKTOP_SWITCHDESKTOP)


def input_is_default(api=None):
    if api is None:
        if not enabled or sys.platform != "win32":
            return True
        api = _DesktopAPI()
    try:
        handle = api.open_input()
    except OSError:
        return False
    try:
        return api.name(handle).lower() == "default"
    except OSError:
        return False
    finally:
        api.close(handle)


class DesktopBinding:

    def __init__(self, api=None):
        self.api = api
        self.handle = None
        self.name = None
        self.original = None
        if api is None and enabled and sys.platform == "win32":
            self.api = _DesktopAPI()
        if self.api is not None:
            self.original = self.api.current()

    def sync(self, before_switch=None):
        if self.api is None:
            return False
        handle = self.api.open_input()
        try:
            name = self.api.name(handle)
            if name == self.name:
                return False
            if before_switch:
                before_switch()
            self.api.attach(handle)
            old, self.handle, self.name = self.handle, handle, name
            handle = None
            if old is not None:
                self.api.close(old)
            return True
        finally:
            if handle is not None:
                self.api.close(handle)

    def close(self):
        if self.api is not None and self.handle is not None:
            self.api.attach(self.original)
            self.api.close(self.handle)
            self.handle = None
            self.name = None


class _DesktopAPI:
    def __init__(self):
        from ctypes import wintypes as w
        self.u = ctypes.WinDLL("user32", use_last_error=True)
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.u.OpenInputDesktop.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.u.OpenInputDesktop.restype = w.HANDLE
        self.u.GetThreadDesktop.argtypes = [w.DWORD]
        self.u.GetThreadDesktop.restype = w.HANDLE
        self.u.SetThreadDesktop.argtypes = [w.HANDLE]
        self.u.SetThreadDesktop.restype = w.BOOL
        self.u.CloseDesktop.argtypes = [w.HANDLE]
        self.u.CloseDesktop.restype = w.BOOL
        self.u.GetUserObjectInformationW.argtypes = [w.HANDLE, ctypes.c_int,
                                                   w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD)]
        self.u.GetUserObjectInformationW.restype = w.BOOL
        self.k.GetCurrentThreadId.restype = w.DWORD

    def current(self):
        return self.u.GetThreadDesktop(self.k.GetCurrentThreadId())

    def open_input(self):
        h = self.u.OpenInputDesktop(0, False, DESKTOP_ACCESS)
        if not h:
            raise ctypes.WinError(ctypes.get_last_error())
        return h

    def name(self, handle):
        buf = ctypes.create_unicode_buffer(256)
        needed = ctypes.c_ulong()
        if not self.u.GetUserObjectInformationW(handle, 2, buf, ctypes.sizeof(buf), ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        return buf.value

    def attach(self, handle):
        if not self.u.SetThreadDesktop(handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self, handle):
        self.u.CloseDesktop(handle)
