import ctypes
import sys
import uuid
from ctypes import HRESULT, POINTER, Structure, byref, c_int, c_longlong, c_ubyte, c_ulong, c_ushort, c_void_p

EDIT, DOCUMENT = 50004, 50030
CONTROL_TYPE, VALUE_PATTERN, VALUE_READ_ONLY = 30003, 30043, 30046
CLSID_CUIAutomation = "ff48dba4-60ef-4201-aa87-54103eef594e"
IID_IUIAutomation = "30cbe57d-d9d0-452a-ab13-7ac5ac4825ee"
VT_I4, VT_BOOL = 3, 11


class _GUID(Structure):
    _fields_ = [("Data1", c_ulong), ("Data2", c_ushort), ("Data3", c_ushort), ("Data4", c_ubyte * 8)]

    @classmethod
    def of(cls, text):
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


class _VARIANT(Structure):
    _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort), ("r3", c_ushort),
                ("data", c_longlong), ("data2", c_longlong)]


def _call(ptr, index, *args, argtypes=()):
    vtable = ctypes.cast(ptr, POINTER(POINTER(c_void_p)))[0]
    return ctypes.WINFUNCTYPE(HRESULT, c_void_p, *argtypes)(vtable[index])(ptr, *args)


def _release(ptr):
    if ptr:
        vtable = ctypes.cast(ptr, POINTER(POINTER(c_void_p)))[0]
        ctypes.WINFUNCTYPE(c_ulong, c_void_p)(vtable[2])(ptr)


class FocusProbe:

    def __init__(self):
        self.uia = c_void_p()
        self.com = False
        if sys.platform != "win32":
            return
        try:
            ctypes.oledll.ole32.CoInitializeEx(None, 0)
            self.com = True
        except OSError:
            pass
        ctypes.oledll.ole32.CoCreateInstance(byref(_GUID.of(CLSID_CUIAutomation)), None, 1,
                                             byref(_GUID.of(IID_IUIAutomation)), byref(self.uia))

    def _value(self, element, prop):
        v = _VARIANT()
        try:
            _call(element, 10, prop, byref(v), argtypes=(c_int, POINTER(_VARIANT)))
            if v.vt == VT_I4:
                return ctypes.c_int32(v.data & 0xFFFFFFFF).value
            if v.vt == VT_BOOL:
                return (v.data & 0xFFFF) != 0
            return None
        finally:
            ctypes.windll.oleaut32.VariantClear(byref(v))

    def _takes_typing(self, element):
        kind = self._value(element, CONTROL_TYPE)
        read_only = self._value(element, VALUE_READ_ONLY)
        if kind == EDIT:
            return read_only is not True
        if kind == DOCUMENT:
            return bool(self._value(element, VALUE_PATTERN)) and read_only is False
        return False

    def window_takes_typing(self, hwnd):
        element = c_void_p()
        try:
            _call(self.uia, 6, c_void_p(hwnd), byref(element), argtypes=(c_void_p, POINTER(c_void_p)))
            return self._takes_typing(element)
        finally:
            _release(element)

    def editable(self):
        if not self.uia:
            return None
        element = c_void_p()
        try:
            _call(self.uia, 8, byref(element), argtypes=(POINTER(c_void_p),))
            return self._takes_typing(element) if element else False
        except OSError:
            return None
        finally:
            _release(element)

    def close(self):
        _release(self.uia)
        self.uia = c_void_p()
        if self.com:
            self.com = False
            ctypes.windll.ole32.CoUninitialize()
