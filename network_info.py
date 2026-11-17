import ipaddress
import os
import shutil
import socket
import subprocess


def is_tailscale(value):
    try:
        address = ipaddress.ip_address(value.split(":", 1)[0])
        return address in ipaddress.ip_network("100.64.0.0/10")
    except (ValueError, TypeError, AttributeError):
        return False


def _tailscale_candidates():
    candidates = [os.path.join(root, "Tailscale", "tailscale.exe") for root in
                  (os.environ.get("ProgramW6432", r"C:\Program Files"),
                   os.environ.get("ProgramFiles", r"C:\Program Files"),
                   os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"))]
    candidates.append(shutil.which("tailscale") or "tailscale")
    return list(dict.fromkeys(candidates))


def _tailscale(*args, timeout=15):
    for executable in _tailscale_candidates():
        try:
            return subprocess.run([executable, *args], capture_output=True, text=True, timeout=timeout,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def tailscale_state():
    import json
    reply = _tailscale("status", "--json", timeout=5)
    if reply is None:
        return None
    try:
        return "on" if json.loads(reply.stdout).get("BackendState") == "Running" else "off"
    except ValueError:
        return "off"


def set_tailscale(on):
    reply = _tailscale("up" if on else "down", timeout=30)
    if reply is None:
        return False, "Tailscale is not installed."
    if reply.returncode != 0:
        return False, (reply.stderr or reply.stdout).strip()[-400:] or f"tailscale exited with {reply.returncode}"
    return True, ""


def local_ips(tailscale=True):
    addresses = []
    try:
        addresses += [info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        pass
    for executable in (_tailscale_candidates() if tailscale else []):
        try:
            reply = subprocess.run([executable, "ip", "-4"], capture_output=True, text=True, timeout=3,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if reply.returncode != 0:
                continue
            found = [value for value in reply.stdout.split() if is_tailscale(value)]
            if found:
                addresses += found
                break
        except (OSError, subprocess.SubprocessError):
            continue
    valid = []
    for value in dict.fromkeys(addresses):
        try:
            ip = ipaddress.IPv4Address(value)
            if not ip.is_loopback and not ip.is_link_local and not ip.is_unspecified:
                valid.append(str(ip))
        except ValueError:
            pass
    return sorted(valid, key=lambda value: not is_tailscale(value))


def status_text(addresses):
    if any(is_tailscale(value) for value in addresses):
        return "Tailscale address detected. Connect using the address above.", "#1a7f37"
    return "Tailscale address not detected yet. Existing direct/VPN connections can still work.", "#777777"


def _process_path(pid):
    import ctypes
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = ctypes.c_void_p
    k.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    k.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p,
                                             ctypes.POINTER(ctypes.c_ulong)]
    h = k.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(len(buf))
        return buf.value if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ""
    finally:
        k.CloseHandle(h)


def port_owners(port):
    if os.name != "nt":
        return []
    import ctypes
    import struct
    iphlp = ctypes.windll.iphlpapi
    size = ctypes.c_ulong(0)
    for _ in range(3):
        buf = ctypes.create_string_buffer(size.value or 4)
        if iphlp.GetExtendedTcpTable(buf, ctypes.byref(size), False, 2, 3, 0) == 0:
            break
    else:
        return []
    owners = []
    count = struct.unpack_from("<I", buf, 0)[0]
    for i in range(count):
        _, _, lport, _, _, pid = struct.unpack_from("<IIIIII", buf, 4 + i * 24)
        if socket.ntohs(lport & 0xFFFF) == port and pid != os.getpid() and pid not in (o[0] for o in owners):
            owners.append((pid, _process_path(pid)))
    return owners


def kill_process(pid):
    import ctypes
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = ctypes.c_void_p
    k.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    k.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k.OpenProcess(0x0001 | 0x00100000, False, pid)
    if not h:
        return False
    try:
        ok = bool(k.TerminateProcess(h, 1))
        if ok:
            k.WaitForSingleObject(h, 5000)
        return ok
    finally:
        k.CloseHandle(h)
