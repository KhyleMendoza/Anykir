import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

SERVICE_NAME = "AnyKir"
VERSION_FILE = "version.txt"
FOLLOW_FILE = "follow.txt"
NETWORK_FIELDS = ("id", "secret", "listen", "hubs", "address", "only_address")


def data_dir():
    return os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "AnyKirService")


def install_dir():
    root = os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles", r"C:\Program Files")
    return os.path.join(root, "AnyKirService")


def installed_follow():
    try:
        with open(os.path.join(install_dir(), FOLLOW_FILE), encoding="utf-8") as f:
            return f.read(1024).strip() or None
    except OSError:
        return None


def installed_version():
    try:
        with open(os.path.join(install_dir(), VERSION_FILE), encoding="ascii") as f:
            text = f.read(32).strip()
        return text if re.fullmatch(r"\d+\.\d+\.\d+", text) else None
    except (OSError, ValueError):
        return None


def read_app_config(path):
    with open(path, "rb") as f:
        data = json.loads(f.read(65536).decode("utf-8"))
    if not isinstance(data, dict):
        return {}
    out = {}
    if re.fullmatch(r"\d{6,12}", str(data.get("id", ""))):
        out["id"] = str(data["id"])
    for key, shortest in (("secret", 16), ("password", 8)):
        v = data.get(key)
        if isinstance(v, str) and shortest <= len(v) <= 256:
            out[key] = v
    v = data.get("listen")
    if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 65535:
        out["listen"] = v
    for key in ("allow_input", "allow_clipboard"):
        if isinstance(data.get(key), bool):
            out[key] = data[key]
    if isinstance(data.get("only_address"), bool):
        out["only_address"] = data["only_address"]
    if isinstance(data.get("address"), str):
        try:
            import ipaddress
            out["address"] = str(ipaddress.IPv4Address(data["address"].strip()))
        except ValueError:
            pass
    if data.get("allow_from_mode") in ("all", "list"):
        out["allow_from_mode"] = data["allow_from_mode"]
    if isinstance(data.get("allow_from"), list):
        from common import clean_allow_list
        out["allow_from"] = clean_allow_list([str(v)[:64] for v in data["allow_from"][:256]])[0]
    if isinstance(data.get("hubs"), list):
        from hub import clean_hubs
        out["hubs"] = clean_hubs(data["hubs"])
    return out


NO_WINDOW = 0x08000000
REMOVE_BLOCK_RULES = ("Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue | "
                      "Where-Object { $_.Program -like '*AnyKir*' } | Get-NetFirewallRule | "
                      "Where-Object { $_.Direction -eq 'Inbound' -and $_.Action -eq 'Block' } | "
                      "Remove-NetFirewallRule")


def _netsh(*args):
    netsh = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "netsh.exe")
    return subprocess.run([netsh, "advfirewall", "firewall", *args], capture_output=True, text=True,
                          creationflags=NO_WINDOW)


def open_firewall(port):
    _netsh("delete", "rule", "name=AnyKir")
    ok = _netsh("add", "rule", "name=AnyKir", "dir=in", "action=allow", "protocol=TCP",
                f"localport={int(port)},7002", "profile=any").returncode == 0
    _netsh("delete", "rule", "name=AnyKir ping")
    _netsh("add", "rule", "name=AnyKir ping", "dir=in", "action=allow", "protocol=icmpv4:8,any", "profile=any")
    powershell = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                              "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    try:
        subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", REMOVE_BLOCK_RULES],
                       capture_output=True, creationflags=NO_WINDOW, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass
    return ok


def firewall_ready(port):
    try:
        out = _netsh("show", "rule", "name=AnyKir").stdout
    except OSError:
        return False
    enabled = re.search(r"^Enabled:\s+Yes", out, re.M)
    ports = re.search(r"^LocalPort:\s+(\S+)", out, re.M)
    return bool(enabled and ports and str(int(port)) in ports.group(1).split(","))


def apply_app_config(service, new):
    from common import derive_key, save_json
    changes = {k: v for k, v in new.items() if service.cfg.get(k) != v}
    if not changes:
        return {}
    service.cfg.update(changes)
    service.key = derive_key(service.cfg["password"], service.cfg["id"])
    save_json("host.json", service.cfg)
    session = getattr(service, "session", None)
    if "allow_input" in changes and session is not None:
        session.injector.enabled = service.cfg["allow_input"]
        if not service.cfg["allow_input"]:
            session.injector.release_all()
    if "listen" in changes:
        try:
            open_firewall(service.cfg["listen"])
        except OSError:
            pass
    if any(k in changes for k in NETWORK_FIELDS):
        service.restart_network()
    return changes


def follow_app(service, interval=5):
    try:
        open_firewall(service.cfg.get("listen") or 7001)
    except OSError:
        pass
    path = service.cfg.get("follow")
    if not path:
        return

    def loop():
        while not service.stopping:
            try:
                apply_app_config(service, read_app_config(path))
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(interval)
    threading.Thread(target=loop, daemon=True).start()


def check_self_update():
    import updater
    from common import VERSION
    with open(os.path.join(data_dir(), "host.json"), encoding="utf-8") as f:
        follow = json.load(f).get("follow")
    if not follow:
        return False
    release = os.path.join(os.path.dirname(follow), "release")
    with open(os.path.join(release, "release.json"), "rb") as f:
        meta = json.loads(f.read(4096).decode("utf-8"))
    version, sig = str(meta.get("version", "")), str(meta.get("sig", ""))
    if not updater.newer(version, VERSION):
        shutil.rmtree(os.path.join(data_dir(), "update"), ignore_errors=True)
        return False
    attempt_file = os.path.join(data_dir(), "update_attempt.json")
    try:
        with open(attempt_file, encoding="utf-8") as f:
            last = json.load(f)
        if last.get("version") == version and time.time() - float(last.get("time", 0)) < 3600:
            return False
    except (OSError, ValueError):
        pass
    folder = os.path.join(data_dir(), "update")
    os.makedirs(folder, exist_ok=True)
    exe = os.path.join(folder, updater.EXE_NAME)
    shutil.copyfile(os.path.join(release, updater.EXE_NAME), exe)
    if not updater.verify(exe, version, sig):
        os.remove(exe)
        return False
    with open(attempt_file, "w", encoding="utf-8") as f:
        json.dump({"version": version, "time": time.time()}, f)
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
    subprocess.Popen([exe, "--service-update", "--host-config", follow], cwd=folder, env=env, close_fds=True,
                     creationflags=0x08000000 | 0x00000008 | 0x00000200)
    return True


def is_system():
    import win32api
    import win32con
    import win32security
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
        return win32security.ConvertSidToStringSid(sid) == "S-1-5-18"
    finally:
        token.Close()


SAS_EVENT = r"Global\AnyKirSAS"
SAS_POLICY_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"


def allow_software_sas():
    import winreg
    try:
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, SAS_POLICY_KEY, 0,
                                winreg.KEY_READ | winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
            try:
                value, _ = winreg.QueryValueEx(key, "SoftwareSASGeneration")
            except OSError:
                value = None
            if value not in (1, 3):
                winreg.SetValueEx(key, "SoftwareSASGeneration", 0, winreg.REG_DWORD, 3)
        return True
    except OSError:
        return False


def send_sas():
    import ctypes
    allow_software_sas()
    ctypes.WinDLL("sas.dll").SendSAS(False)


def request_sas():
    import win32event
    try:
        event = win32event.OpenEvent(0x0002, False, SAS_EVENT)
    except Exception:
        send_sas()
        return
    try:
        win32event.SetEvent(event)
    finally:
        event.Close()


def _sas_event():
    import pywintypes
    import win32event
    import win32security
    sa = pywintypes.SECURITY_ATTRIBUTES()
    sa.SECURITY_DESCRIPTOR = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        "D:P(A;;GA;;;SY)", win32security.SDDL_REVISION_1)
    return win32event.CreateEvent(sa, False, False, SAS_EVENT)


def run_agent():
    if not is_system():
        raise PermissionError("The UAC agent must be started by the installed AnyKir service.")
    from common import set_config_dir
    set_config_dir(data_dir())
    if not os.path.isfile(os.path.join(data_dir(), "host.json")):
        raise RuntimeError("Service configuration is missing. Click Install admin service in AnyKir first.")
    import windows_desktop
    windows_desktop.enabled = True
    import host
    host.main(["--nogui", "--service-agent"])


def stop_everything():
    import win32serviceutil
    taskkill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
    try:
        subprocess.run([taskkill, "/F", "/T", "/IM", "AnyKir.exe"], capture_output=True,
                       creationflags=0x08000000, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        win32serviceutil.StopService(SERVICE_NAME)
    except Exception:
        pass


def console_session():
    import win32ts
    session = win32ts.WTSGetActiveConsoleSessionId()
    if session in (0, 0xFFFFFFFF):
        return None
    return session


def launch_agent(session):
    import win32api
    import win32con
    import win32process
    import win32profile
    import win32security
    import win32job
    source = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_ALL_ACCESS)
    try:
        privileges = [(win32security.LookupPrivilegeValue(None, name), win32con.SE_PRIVILEGE_ENABLED)
                      for name in ("SeTcbPrivilege", "SeAssignPrimaryTokenPrivilege", "SeIncreaseQuotaPrivilege")]
        win32security.AdjustTokenPrivileges(source, False, privileges)
        token = win32security.DuplicateTokenEx(source, win32security.SecurityImpersonation,
                                               win32con.TOKEN_ALL_ACCESS, win32security.TokenPrimary)
        try:
            win32security.SetTokenInformation(token, win32security.TokenSessionId, session)
            startup = win32process.STARTUPINFO()
            startup.lpDesktop = r"winsta0\default"
            env = win32profile.CreateEnvironmentBlock(token, False)
            env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
            args = [sys.executable, "--service-agent"]
            if not getattr(sys, "frozen", False):
                args.insert(1, os.path.join(os.path.dirname(__file__), "anykir.py"))
            process, thread, _, _ = win32process.CreateProcessAsUser(
                token, sys.executable, subprocess.list2cmdline(args), None, None, False,
                win32con.CREATE_UNICODE_ENVIRONMENT | win32con.CREATE_NO_WINDOW | win32con.CREATE_SUSPENDED,
                env, os.path.dirname(sys.executable), startup)
            job = None
            try:
                job = win32job.CreateJobObject(None, "")
                limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
                limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
                win32job.AssignProcessToJobObject(job, process)
                win32process.ResumeThread(thread)
                return process, job
            except Exception:
                win32process.TerminateProcess(process, 1)
                process.Close()
                if job is not None:
                    job.Close()
                raise
            finally:
                thread.Close()
        finally:
            token.Close()
    finally:
        source.Close()


class AgentSupervisor:

    def __init__(self, launch, alive, terminate):
        self.launch, self.alive, self.terminate = launch, alive, terminate
        self.process = None
        self.session = None

    def step(self, session):
        if self.process is not None and (session != self.session or not self.alive(self.process)):
            self.stop()
        if session is not None and self.process is None:
            self.process = self.launch(session)
            self.session = session

    def stop(self):
        if self.process is not None:
            self.terminate(self.process)
            self.process = None
            self.session = None


def dispatch():
    import servicemanager
    import win32event
    import win32process
    import win32service
    import win32serviceutil

    class AnyKirService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = "AnyKir administrator access"
        _svc_description_ = "Hosts authenticated AnyKir sessions with access to administrator windows and UAC prompts."

        def __init__(self, args):
            super().__init__(args)
            self.stop_event = win32event.CreateEvent(None, True, False, None)

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.stop_event)

        def SvcShutdown(self):
            self.SvcStop()

        def SvcDoRun(self):
            def alive(process):
                return win32event.WaitForSingleObject(process[0], 0) == win32event.WAIT_TIMEOUT

            def terminate(process):
                try:
                    process[1].Close()
                    win32event.WaitForSingleObject(process[0], 5000)
                finally:
                    process[0].Close()

            supervisor = AgentSupervisor(launch_agent, alive, terminate)
            last_error = None
            try:
                from common import VERSION
                with open(os.path.join(install_dir(), VERSION_FILE), "w", encoding="ascii") as f:
                    f.write(VERSION)
                with open(os.path.join(data_dir(), "host.json"), encoding="utf-8") as f:
                    follow = str(json.load(f).get("follow") or "")
                with open(os.path.join(install_dir(), FOLLOW_FILE), "w", encoding="utf-8") as f:
                    f.write(follow)
            except (OSError, ValueError):
                pass
            allow_software_sas()

            def clean():
                try:
                    import cleanup
                    cleanup.clean_temp(cleanup.system_temp_dirs())
                    cleanup.clean_setup_leftovers()
                except Exception:
                    pass
            threading.Thread(target=clean, daemon=True).start()
            try:
                sas = _sas_event()
            except Exception:
                sas = None
            events = [self.stop_event] + ([sas] if sas else [])
            ticks = 0
            try:
                while True:
                    woke = win32event.WaitForMultipleObjects(events, False, 2000)
                    if woke == win32event.WAIT_OBJECT_0:
                        break
                    if woke == win32event.WAIT_OBJECT_0 + 1:
                        try:
                            send_sas()
                        except Exception as exc:
                            servicemanager.LogErrorMsg("AnyKir Ctrl+Alt+Del: " + str(exc))
                        continue
                    ticks += 1
                    if ticks % 30 == 5:
                        try:
                            check_self_update()
                        except Exception:
                            pass
                    try:
                        supervisor.step(console_session())
                        last_error = None
                    except Exception as exc:
                        import traceback
                        message = str(exc) + "\n" + traceback.format_exc()
                        if message != last_error:
                            servicemanager.LogErrorMsg("AnyKir desktop agent: " + message)
                            last_error = message
            finally:
                supervisor.stop()
                if sas:
                    sas.Close()
                self.stop_event.Close()

    if not is_system():
        raise PermissionError("Start AnyKir through Windows Services, after administrator installation.")
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(AnyKirService)
    servicemanager.StartServiceCtrlDispatcher()
