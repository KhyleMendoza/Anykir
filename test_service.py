import ast
import io
import pathlib
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import windows_desktop
from windows_service import AgentSupervisor


class FakeDesktop:
    def __init__(self):
        self.selected = "original"
        self.input = "Default"
        self.opened = 0
        self.closed = []
        self.fail_attach = False
        self.attaching_threads = []

    def current(self):
        return self.selected

    def open_input(self):
        self.opened += 1
        return (self.opened, self.input)

    def name(self, handle):
        return handle[1]

    def attach(self, handle):
        if self.fail_attach:
            raise OSError("access denied")
        self.attaching_threads.append(threading.get_ident())
        self.selected = handle

    def close(self, handle):
        self.closed.append(handle)


class DesktopTests(unittest.TestCase):
    def test_default_uac_default_transition_closes_each_handle(self):
        api = FakeDesktop()
        binding = windows_desktop.DesktopBinding(api)
        release_capture = Mock()
        for desktop in ("Default", "Winlogon", "Default"):
            api.input = desktop
            self.assertTrue(binding.sync(release_capture))
            self.assertEqual(binding.name, desktop)
        self.assertEqual(release_capture.call_count, 3)
        self.assertFalse(binding.sync(release_capture))
        self.assertEqual(release_capture.call_count, 3)
        binding.close()
        self.assertEqual(api.selected, "original")
        self.assertEqual(len(api.closed), api.opened)

    def test_resources_release_before_switch_on_calling_thread(self):
        api = FakeDesktop()
        binding = windows_desktop.DesktopBinding(api)
        previous = api.selected
        binding.sync(lambda: self.assertEqual(api.selected, previous))
        self.assertEqual(api.attaching_threads, [threading.get_ident()])
        binding.close()

    def test_failed_attach_retains_existing_binding_and_closes_candidate(self):
        api = FakeDesktop()
        binding = windows_desktop.DesktopBinding(api)
        binding.sync()
        old = binding.handle
        api.input = "Winlogon"
        api.fail_attach = True
        with self.assertRaises(OSError):
            binding.sync()
        self.assertEqual(binding.handle, old)
        self.assertEqual(binding.name, "Default")
        self.assertNotIn(old, api.closed)
        self.assertIn((2, "Winlogon"), api.closed)
        api.fail_attach = False
        binding.close()

    def test_portable_mode_does_not_touch_desktops(self):
        with patch.object(windows_desktop, "enabled", False), patch.object(windows_desktop, "_DesktopAPI") as api:
            binding = windows_desktop.DesktopBinding()
            self.assertFalse(binding.sync())
            binding.close()
            api.assert_not_called()


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.launch = Mock(side_effect=lambda session: object())
        self.alive = Mock(return_value=True)
        self.stop = Mock()
        self.supervisor = AgentSupervisor(self.launch, self.alive, self.stop)

    def test_no_user_no_agent(self):
        self.supervisor.step(None)
        self.launch.assert_not_called()

    def test_same_session_preserves_agent(self):
        self.supervisor.step(1)
        process = self.supervisor.process
        self.supervisor.step(1)
        self.assertIs(process, self.supervisor.process)
        self.launch.assert_called_once_with(1)
        self.stop.assert_not_called()

    def test_session_switch_terminates_old_agent_before_launching_new_one(self):
        calls = []
        self.supervisor = AgentSupervisor(lambda s: calls.append(("launch", s)) or s,
                                         lambda p: True, lambda p: calls.append(("stop", p)))
        self.supervisor.step(1)
        self.supervisor.step(2)
        self.assertEqual(calls, [("launch", 1), ("stop", 1), ("launch", 2)])

    def test_logoff_stops_agent(self):
        self.supervisor.step(1)
        process = self.supervisor.process
        self.supervisor.step(None)
        self.stop.assert_called_once_with(process)
        self.assertIsNone(self.supervisor.process)

    def test_crashed_agent_restarts(self):
        self.supervisor.step(1)
        self.alive.return_value = False
        self.supervisor.step(1)
        self.assertEqual(self.launch.call_count, 2)
        self.assertEqual(self.stop.call_count, 1)

    def test_stop_idempotent(self):
        self.supervisor.step(1)
        self.supervisor.stop()
        self.supervisor.stop()
        self.stop.assert_called_once()

    def test_failed_launch_can_retry(self):
        self.launch.side_effect = [OSError("session changing"), object()]
        with self.assertRaises(OSError):
            self.supervisor.step(1)
        self.assertIsNone(self.supervisor.process)
        self.supervisor.step(1)
        self.assertIsNotNone(self.supervisor.process)


class HostIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import host
        cls.host = host

    def session(self):
        session = object.__new__(self.host.HostSession)
        session.service = SimpleNamespace(cfg={"allow_files": False, "allow_clipboard": False})
        session.ch = Mock()
        session.injector = Mock()
        session.input_desktop = Mock()
        return session

    def test_input_attaches_before_injection(self):
        session = self.session()
        calls = []
        session.input_desktop.sync.side_effect = lambda **kw: calls.append("attach")
        session.injector.on_mouse.side_effect = lambda m: calls.append("input")
        session._handle({"t": "mouse", "x": 0.5, "y": 0.5})
        self.assertEqual(calls, ["attach", "input"])

    def test_desktop_access_denied_drops_input(self):
        session = self.session()
        session.input_desktop.sync.side_effect = OSError("access denied")
        session._handle({"t": "key", "k": "x", "a": "down"})
        session.injector.on_key.assert_not_called()

    def test_ctrl_alt_del_goes_to_the_service(self):
        import windows_service
        for message in ({"t": "cad"}, {"t": "combo", "keys": ["ctrl", "alt", "delete"]}):
            session = self.session()
            with patch.object(windows_desktop, "enabled", True), \
                    patch.object(windows_service, "request_sas") as request:
                session._handle(message)
            request.assert_called_once()
            session.injector.on_combo.assert_not_called()

    def test_ctrl_alt_del_without_service_explains(self):
        import windows_service
        session = self.session()
        with patch.object(windows_desktop, "enabled", False), \
                patch.object(windows_service, "request_sas") as request:
            session._handle({"t": "cad"})
        request.assert_not_called()
        self.assertIn("admin service", session.ch.send_json.call_args.args[0]["text"])

    def test_sas_policy_enabled_only_when_off(self):
        import windows_service
        for current, written in ((None, 3), (0, 3), (2, 3), (1, None), (3, None)):
            key = Mock()
            key.__enter__ = Mock(return_value=key)
            key.__exit__ = Mock(return_value=False)
            with patch("winreg.CreateKeyEx", return_value=key), \
                    patch("winreg.QueryValueEx", side_effect=OSError if current is None else None,
                          return_value=(current, 4)), \
                    patch("winreg.SetValueEx") as set_value:
                self.assertTrue(windows_service.allow_software_sas())
            if written is None:
                set_value.assert_not_called()
            else:
                self.assertEqual(set_value.call_args.args[-1], written)

    def test_service_file_requests_refused_before_filesystem_operation(self):
        session = self.session()
        session._begin_upload = Mock()
        session._list_dir = Mock()
        session._send_file = Mock()
        for kind in ("ls", "get", "put", "put_end"):
            session._handle({"t": kind, "path": "C:\\Windows", "name": "file"})
            self.assertFalse(session.ch.send_json.call_args.args[0]["ok"])
        session._begin_upload.assert_not_called()
        session._list_dir.assert_not_called()
        session._send_file.assert_not_called()

    def test_clipboard_not_shared_from_lock_screen(self):
        class Api:
            def __init__(self, name):
                self.desk = name

            def open_input(self):
                return 1

            def name(self, handle):
                return self.desk

            def close(self, handle):
                pass

        self.assertTrue(windows_desktop.input_is_default(Api("Default")))
        self.assertFalse(windows_desktop.input_is_default(Api("Winlogon")))

    def test_clipboard_disabled_in_app_is_refused(self):
        session = self.session()
        with patch.object(self.host.pyperclip, "copy") as copy:
            session._handle({"t": "clip", "text": "test"})
            copy.assert_not_called()

    def test_service_updates_refused_even_if_configuration_enables_them(self):
        session = self.session()
        session.service.cfg["allow_update"] = True
        session.upd = Mock()
        with patch.object(windows_desktop, "enabled", True):
            session._begin_update({"ver": "9.9.9"})
        session.upd.start.assert_not_called()
        self.assertFalse(session.ch.send_json.call_args.args[0]["ok"])

    def test_service_capture_uses_desktop_switchable_gdi(self):
        with patch.object(windows_desktop, "enabled", True), patch.object(self.host, "MSS"):
            grabber = self.host.Grabber()
            self.assertFalse(grabber.dx_ok)
            grabber.close()

    def test_service_serves_signed_releases_without_auto_update_watcher(self):
        service = object.__new__(self.host.HostService)
        service.restart_network = Mock()
        with patch.object(windows_desktop, "enabled", True), \
                patch.object(self.host.updater, "ensure_server") as server, \
                patch.object(self.host.updater, "Watcher") as watcher:
            service.start()
            server.assert_called_once()
            watcher.assert_not_called()

    def test_disabling_input_revokes_active_session(self):
        app = object.__new__(self.host.HostApp)
        injector = Mock()
        app.svc = SimpleNamespace(cfg={}, session=SimpleNamespace(injector=injector))
        app.input_var = SimpleNamespace(get=lambda: False)
        with patch.object(self.host, "save_json"):
            app._save_input()
        self.assertFalse(injector.enabled)
        injector.release_all.assert_called_once()


class DesktopAccessTests(unittest.TestCase):
    def test_input_desktop_opened_with_right_to_inject_input(self):
        self.assertTrue(windows_desktop.DESKTOP_ACCESS & windows_desktop.DESKTOP_JOURNALPLAYBACK)
        api = windows_desktop._DesktopAPI()
        with patch.object(api.u, "OpenInputDesktop", return_value=1) as open_desktop:
            api.open_input()
        self.assertEqual(open_desktop.call_args[0][2], windows_desktop.DESKTOP_ACCESS)


class YieldToServiceTests(unittest.TestCase):
    def test_app_only_gives_way_to_a_service_that_follows_its_settings(self):
        import os
        import service_setup
        import windows_service
        mine = os.path.join(service_setup.config_dir(), "host.json")
        for follow, expected in ((mine, True), (mine.upper(), True), (r"C:\Users\other\AppData\Roaming\AnyKir\host.json", False),
                                 (None, False)):
            with patch.object(windows_service, "installed_follow", return_value=follow):
                self.assertEqual(service_setup.follows_this_app(), expected, follow)


class HubTests(unittest.TestCase):
    def test_server_list_and_match(self):
        import hub
        self.assertEqual(hub.clean_hubs("192.168.210.112, bad host!, x:7000, x:7000"), ["192.168.210.112", "x:7000"])
        hosts = [{"id": "1", "name": "LNK100", "peer": "192.168.204.17", "ips": ["192.168.210.58"], "port": 7001}]
        self.assertEqual(hub.match(hosts, "192.168.204.17:7001")["id"], "1")
        self.assertEqual(hub.match(hosts, "192.168.210.58")["id"], "1")
        self.assertEqual(hub.match(hosts, "lnk100")["id"], "1")
        self.assertIsNone(hub.match(hosts, "10.0.0.9"))

    def test_host_calls_home_and_viewer_reaches_it_through_the_server(self):
        import socket
        import threading
        import time
        import hub
        srv = hub.Hub(0)
        srv.port = 0
        self.assertTrue(srv.start())
        port = srv.srv.getsockname()[1]
        served = threading.Event()
        seen = {}

        def serve(sock, label, ip=None):
            seen["ip"] = ip
            self.assertEqual(sock.recv(5), b"hello")
            sock.sendall(b"world")
            sock.close()
            served.set()
        alive = {"on": True}
        hub.call_home(f"127.0.0.1:{port}", lambda: {"id": "123456789", "name": "pc", "port": 7001},
                      serve, lambda: alive["on"])
        for _ in range(40):
            if srv.online():
                break
            time.sleep(0.1)
        self.assertEqual([e["name"] for e in hub.fetch_online(f"127.0.0.1:{port}")], ["pc"])
        s = hub.connect_via(f"127.0.0.1:{port}", "123456789")
        s.sendall(b"hello")
        self.assertEqual(s.recv(5), b"world")
        self.assertTrue(served.wait(5))
        self.assertEqual(seen["ip"], "127.0.0.1", "the server tells the PC who the viewer is")
        s.close()
        with self.assertRaises(ConnectionError):
            hub.connect_via(f"127.0.0.1:{port}", "999999999")
        alive["on"] = False
        srv.stop()


class ServerControlTests(unittest.TestCase):
    def test_disconnect_disable_enable(self):
        import time
        import hub
        srv = hub.Hub(0)
        self.assertTrue(srv.start())
        port = srv.srv.getsockname()[1]
        alive = {"on": True}
        hello = lambda: {"id": "123456789", "name": "pc", "port": 7001}

        def serve(sock, label, ip=None):
            while sock.recv(1024):
                pass

        def wait(cond, secs=20):
            end = time.time() + secs
            while time.time() < end and not cond():
                time.sleep(0.1)
            return cond()
        with patch.object(hub, "REFUSED_WAIT", 1):
            line = hub.call_home(f"127.0.0.1:{port}", hello, serve, lambda: alive["on"])
            self.assertTrue(wait(lambda: srv.online()))
            viewer = hub.connect_via(f"127.0.0.1:{port}", "123456789")
            self.assertTrue(wait(lambda: srv.sessions("123456789") == 1))

            srv.kick("123456789")
            self.assertEqual(viewer.recv(10), b"")
            self.assertTrue(wait(lambda: srv.sessions("123456789") == 0))
            self.assertTrue(wait(lambda: srv.online()))

            srv.disable("123456789", "pc", "127.0.0.1")
            self.assertTrue(wait(lambda: line["refused"]))
            self.assertEqual(srv.online(), [])
            with self.assertRaises(ConnectionError):
                hub.connect_via(f"127.0.0.1:{port}", "123456789")
            self.assertGreater(srv.blocked["123456789"]["tried"], 0)

            srv.enable("123456789")
            self.assertTrue(wait(lambda: srv.online()))
        alive["on"] = False
        srv.stop()

    def test_server_disconnect_stops_anykir_on_the_pc(self):
        import threading
        import time
        import hub
        srv = hub.Hub(0)
        self.assertTrue(srv.start())
        port = srv.srv.getsockname()[1]
        stopped = threading.Event()
        alive = {"on": True}
        line = hub.call_home(f"127.0.0.1:{port}", lambda: {"id": "123456789", "name": "pc", "port": 7001},
                             lambda s, l: None, lambda: alive["on"], on_stop=stopped.set)
        end = time.time() + 8
        while time.time() < end and not srv.online():
            time.sleep(0.1)
        srv.stop_pc("123456789")
        self.assertTrue(stopped.wait(5))
        self.assertEqual(srv.online(), [])
        end = time.time() + 3
        while time.time() < end and not line["refused"]:
            time.sleep(0.05)
        self.assertTrue(line["refused"])
        alive["on"] = False
        srv.stop()

    def test_remote_stop_on_service_closes_app_and_stops_service(self):
        import windows_service
        with patch("subprocess.run") as run, patch("win32serviceutil.StopService") as stop:
            windows_service.stop_everything()
        self.assertIn("AnyKir.exe", run.call_args.args[0])
        stop.assert_called_once_with("AnyKir")


class InstallTests(unittest.TestCase):
    def test_new_install_takes_the_apps_settings(self):
        import service_setup
        app = {"id": "123456789", "secret": "s" * 24, "password": "LongEnough1", "listen": 7005,
               "allow_input": True, "allow_files": True}
        cfg = service_setup.service_config({}, app, r"C:\Users\x\AppData\Roaming\AnyKir\host.json")
        self.assertEqual((cfg["id"], cfg["secret"], cfg["password"], cfg["listen"]),
                         ("123456789", "s" * 24, "LongEnough1", 7005))
        self.assertTrue(cfg["follow"].endswith("host.json"))
        self.assertFalse(cfg["allow_files"] or cfg["allow_update"])
        self.assertTrue(cfg["allow_clipboard"])
        cfg = service_setup.service_config({}, dict(app, allow_clipboard=False), None)
        self.assertFalse(cfg["allow_clipboard"])

    def test_reinstall_keeps_service_values_when_app_has_none(self):
        import service_setup
        existing = {"id": "987654321", "secret": "e" * 24, "password": "ServicePass", "follow": "old.json",
                    "allow_input": False}
        cfg = service_setup.service_config(existing, {}, None)
        self.assertEqual((cfg["id"], cfg["password"], cfg["follow"], cfg["allow_input"]),
                         ("987654321", "ServicePass", "old.json", False))

    def test_bad_values_are_ignored(self):
        import service_setup
        cfg = service_setup.service_config({}, {"id": "12ab", "password": "short", "listen": "7001"}, None)
        self.assertNotEqual(cfg["password"], "short")
        self.assertEqual(cfg["listen"], 7001)
        self.assertRegex(cfg["id"], r"^\d{9}$")

    def test_folder_permissions(self):
        import service_setup
        self.assertIn("(A;OICI;FA;;;SY)", service_setup.DATA_SDDL)
        self.assertNotIn("BU", service_setup.DATA_SDDL)
        self.assertIn("(A;OICI;0x1200a9;;;BU)", service_setup.INSTALL_SDDL)
        self.assertTrue(service_setup.INSTALL_SDDL.startswith("O:BAD:P"))

    def test_no_powershell_script_is_used(self):
        import service_setup
        source = pathlib.Path("service_setup.py").read_text(encoding="utf-8")
        self.assertNotIn(".ps1", source)
        self.assertNotIn("ExecutionPolicy", source)
        self.assertIsInstance(service_setup._bundle(), str)


class AppPasswordTests(unittest.TestCase):
    def test_only_the_hashed_password_unlocks(self):
        import base64
        import hashlib
        import common
        salt = b"0123456789abcdef"
        digest = hashlib.pbkdf2_hmac("sha256", b"right-one", salt, 1000)
        with patch.object(common, "APP_LOCK_SALT", base64.b64encode(salt).decode()), \
                patch.object(common, "APP_LOCK_HASH", base64.b64encode(digest).decode()), \
                patch.object(common, "APP_LOCK_ROUNDS", 1000):
            self.assertTrue(common.check_app_password("right-one"))
            self.assertFalse(common.check_app_password("wrong"))
            self.assertFalse(common.check_app_password(""))
            self.assertFalse(common.check_app_password(None))

    def test_second_open_reuses_the_password_box(self):
        import common
        box, entry = Mock(), Mock()
        box.winfo_exists.return_value = True
        with patch.dict(common._unlock_box, {"win": box, "entry": entry}), patch("tkinter.Toplevel") as toplevel:
            self.assertFalse(common.unlock(Mock(), "open"))
        toplevel.assert_not_called()
        box.lift.assert_called_once()
        entry.focus_force.assert_called_once()

    def test_password_is_not_stored_in_plain_text(self):
        source = pathlib.Path("common.py").read_text(encoding="utf-8")
        self.assertIn("APP_LOCK_HASH", source)
        self.assertNotIn("check_app_password(\"", source)


class FollowAppTests(unittest.TestCase):

    def _service(self, **cfg):
        base = {"id": "123456789", "secret": "s" * 24, "password": "oldpassword",
                "listen": 7001, "allow_input": True, "allow_files": False}
        base.update(cfg)
        return SimpleNamespace(cfg=base, key=b"", restart_network=Mock(), stopping=False)

    def _app_file(self, data):
        import json
        import tempfile
        d = tempfile.mkdtemp()
        path = pathlib.Path(d, "host.json")
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    def test_password_change_applies_without_network_restart(self):
        import windows_service
        svc = self._service()
        with patch("common.save_json") as save:
            changed = windows_service.apply_app_config(svc, {"password": "password123"})
        self.assertEqual(changed, {"password": "password123"})
        self.assertEqual(svc.cfg["password"], "password123")
        self.assertNotEqual(svc.key, b"")
        save.assert_called_once()
        svc.restart_network.assert_not_called()

    def test_port_change_restarts_network(self):
        import windows_service
        svc = self._service()
        with patch("common.save_json"), patch.object(windows_service, "open_firewall") as fw:
            windows_service.apply_app_config(svc, {"listen": 7005})
        svc.restart_network.assert_called_once()
        fw.assert_called_once_with(7005)

    def test_firewall_rule_allows_port_feed_and_ping_and_drops_block_rules(self):
        import windows_service
        with patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="")) as run:
            self.assertTrue(windows_service.open_firewall(7001))
        calls = [" ".join(c.args[0]) for c in run.call_args_list]
        self.assertTrue(any("name=AnyKir dir=in action=allow protocol=TCP localport=7001,7002 profile=any" in c
                            for c in calls))
        self.assertTrue(any("name=AnyKir ping" in c and "icmpv4:8,any" in c and " add " in f" {c} " for c in calls))
        self.assertTrue(any("Remove-NetFirewallRule" in c and "Block" in c for c in calls))

    def test_firewall_ready_reads_the_rule(self):
        import windows_service
        shown = "Enabled:  Yes\nLocalPort:   7001,7002\nAction:  Allow\n"
        with patch.object(windows_service, "_netsh", return_value=SimpleNamespace(stdout=shown)):
            self.assertTrue(windows_service.firewall_ready(7001))
            self.assertFalse(windows_service.firewall_ready(7005))
        with patch.object(windows_service, "_netsh", return_value=SimpleNamespace(stdout="No rules match")):
            self.assertFalse(windows_service.firewall_ready(7001))

    def test_no_change_does_nothing(self):
        import windows_service
        svc = self._service()
        with patch("common.save_json") as save:
            self.assertEqual(windows_service.apply_app_config(svc, {"password": "oldpassword"}), {})
        save.assert_not_called()

    def test_access_rights_and_bad_values_are_ignored(self):
        import windows_service
        path = self._app_file({"password": "short", "id": "12ab", "allow_files": True, "allow_update": True,
                               "allow_clipboard": True, "listen": "7001", "relay": "r:7000", "allow_input": "yes",
                               "secret": "s" * 24})
        self.assertEqual(windows_service.read_app_config(path), {"secret": "s" * 24, "allow_clipboard": True})

    def test_remote_control_switch_follows_app_live(self):
        import windows_service
        svc = self._service(allow_input=False)
        svc.session = SimpleNamespace(injector=SimpleNamespace(enabled=False, release_all=Mock()))
        path = self._app_file({"allow_input": True})
        with patch("common.save_json"):
            windows_service.apply_app_config(svc, windows_service.read_app_config(path))
        self.assertTrue(svc.cfg["allow_input"])
        self.assertTrue(svc.session.injector.enabled)
        with patch("common.save_json"):
            windows_service.apply_app_config(svc, {"allow_input": False})
        self.assertFalse(svc.session.injector.enabled)
        svc.session.injector.release_all.assert_called_once()

    def test_never_left_unreachable(self):
        import windows_service
        svc = self._service()
        with patch("common.save_json"):
            self.assertEqual(windows_service.apply_app_config(
                svc, windows_service.read_app_config(self._app_file({"listen": 0}))), {})
        self.assertEqual(svc.cfg["listen"], 7001)


class SelfUpdateTests(unittest.TestCase):
    def _setup(self, version, signed=True):
        import json
        import tempfile
        data, app = tempfile.mkdtemp(), tempfile.mkdtemp()
        pathlib.Path(data, "host.json").write_text(json.dumps({"follow": str(pathlib.Path(app, "host.json"))}))
        rel = pathlib.Path(app, "release")
        rel.mkdir()
        (rel / "AnyKir.exe").write_bytes(b"release bytes")
        (rel / "release.json").write_text(json.dumps({"version": version, "sig": "x"}))
        return data, patch("updater.verify", return_value=signed)

    def test_newer_signed_release_starts_update(self):
        import windows_service
        data, verify = self._setup("99.0.0")
        with patch.object(windows_service, "data_dir", return_value=data), verify, \
                patch("subprocess.Popen") as popen:
            self.assertTrue(windows_service.check_self_update())
            self.assertFalse(windows_service.check_self_update())
        args = popen.call_args[0][0]
        self.assertIn("--service-update", args)
        self.assertTrue(args[0].startswith(data))

    def test_unsigned_or_older_release_ignored(self):
        import windows_service
        for version, signed in (("99.0.0", False), ("0.0.1", True)):
            data, verify = self._setup(version, signed)
            with patch.object(windows_service, "data_dir", return_value=data), verify, \
                    patch("subprocess.Popen") as popen:
                self.assertFalse(windows_service.check_self_update())
            popen.assert_not_called()


class EntrypointTests(unittest.TestCase):
    def test_launcher_import_does_not_load_desktop_libraries(self):
        tree = ast.parse(pathlib.Path("anykir.py").read_text(encoding="utf-8"))
        imports = {alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names}
        self.assertNotIn("host", imports)
        self.assertNotIn("viewer", imports)

    def test_service_agent_rejects_non_system_launch(self):
        import windows_service
        with patch.object(windows_service, "is_system", return_value=False):
            with self.assertRaises(PermissionError):
                windows_service.run_agent()

    def test_is_system_runs_real_token_path(self):
        import windows_service
        self.assertIsInstance(windows_service.is_system(), bool)


if __name__ == "__main__":
    unittest.main()


class CleanupTests(unittest.TestCase):
    def test_only_old_unused_anykir_unpack_folders_are_deleted(self):
        import os
        import tempfile
        import time
        import cleanup
        root = tempfile.mkdtemp()

        def unpack(name, marker=True, age=3600):
            path = os.path.join(root, name)
            os.makedirs(os.path.join(path, "svc"))
            with open(os.path.join(path, "big.bin"), "wb") as f:
                f.write(b"x" * 1000)
            if marker:
                open(os.path.join(path, "svc", "AnyKirSvc.zip"), "wb").close()
            then = time.time() - age
            os.utime(path, (then, then))
            return path

        old = unpack("_MEI1001")
        fresh = unpack("_MEI1002", age=30)
        other_app = unpack("_MEI1003", marker=False)
        busy = unpack("_MEI1004")
        held = open(os.path.join(busy, "big.bin"), "rb")
        try:
            freed = cleanup.clean_temp([root])
        finally:
            held.close()
        self.assertFalse(os.path.exists(old))
        self.assertGreaterEqual(freed, 1000)
        for kept in (fresh, other_app, busy):
            self.assertTrue(os.path.isdir(kept), kept)

    def test_frozen_service_kill_spares_the_update_process(self):
        import service_setup
        with patch("subprocess.run") as run:
            service_setup.kill_service_processes()
        args = run.call_args.args[0]
        self.assertIn("AnyKirSvc.exe", args)
        self.assertNotIn("/T", args)


class FastCopyTests(unittest.TestCase):

    def setUp(self):
        import os
        import sys
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.meipass = os.path.join(self.tmp, "Temp", "_MEI12345")
        os.makedirs(os.path.join(self.meipass, "svc"))
        os.makedirs(os.path.join(self.meipass, "PIL"))
        for rel in ("python312.dll", "base_library.zip", os.path.join("PIL", "_imaging.pyd"),
                    os.path.join("svc", "AnyKirSvc.exe")):
            with open(os.path.join(self.meipass, rel), "w") as f:
                f.write(rel)
        self.home = os.path.join(self.tmp, "Downloads", "AnyKir.exe")
        os.makedirs(os.path.dirname(self.home))
        open(self.home, "w").close()
        self.patches = [patch.object(sys, "frozen", True, create=True),
                        patch.object(sys, "_MEIPASS", self.meipass, create=True),
                        patch.object(sys, "executable", self.home),
                        patch.dict(os.environ, {"LOCALAPPDATA": os.path.join(self.tmp, "Local")})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        import shutil
        for p in reversed(self.patches):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_makes_a_ready_folder_copy_once(self):
        import os
        import fastcopy
        self.assertTrue(fastcopy.is_onefile())
        self.assertFalse(fastcopy.is_fast())
        self.assertIsNone(fastcopy.exe())
        self.assertEqual(fastcopy.launcher(), self.home)
        target = fastcopy.ensure()
        folder = fastcopy.folder()
        self.assertEqual(target, os.path.join(folder, "AnyKir.exe"))
        with open(target) as f:
            self.assertEqual(f.read(), os.path.join("svc", "AnyKirSvc.exe"))
        self.assertTrue(os.path.isfile(os.path.join(folder, "_internal", "PIL", "_imaging.pyd")))
        self.assertTrue(os.path.isfile(os.path.join(folder, "_internal", "svc", "AnyKirSvc.exe")))
        self.assertEqual(fastcopy.launcher(), target)
        self.assertEqual(fastcopy.ensure(), target)

    def test_the_copy_knows_it_is_fast_and_where_it_came_from(self):
        import os
        import sys
        import fastcopy
        target = fastcopy.ensure()
        with patch.object(sys, "executable", target), \
                patch.object(sys, "_MEIPASS", os.path.join(os.path.dirname(target), "_internal")):
            self.assertFalse(fastcopy.is_onefile())
            self.assertTrue(fastcopy.is_fast())
            self.assertEqual(fastcopy.home_exe(), self.home)
            self.assertEqual(fastcopy.launcher(), target)

    def test_incomplete_copy_is_never_used(self):
        import os
        import fastcopy
        fastcopy.ensure()
        os.remove(os.path.join(fastcopy.folder(), fastcopy.DONE))
        self.assertIsNone(fastcopy.exe())

    def test_old_versions_are_removed_unless_running(self):
        import os
        import fastcopy
        old, busy = os.path.join(fastcopy.root(), "1.0.1"), os.path.join(fastcopy.root(), "1.0.2")
        for path in (old, busy):
            os.makedirs(os.path.join(path, "_internal"))
            open(os.path.join(path, "_internal", "python312.dll"), "w").close()
        with open(os.path.join(busy, "_internal", "python312.dll")) as locked, \
                patch.object(fastcopy.os, "remove", side_effect=lambda p: (_ for _ in ()).throw(
                    PermissionError("in use")) if p.startswith(busy) else os.unlink(p)):
            fastcopy.ensure()
            self.assertIsNotNone(locked)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(busy))
        self.assertIsNotNone(fastcopy.exe())

    def test_service_is_staged_from_the_apps_own_files(self):
        import os
        import tempfile
        import service_setup
        staging = tempfile.mkdtemp(dir=self.tmp)
        source = service_setup.stage_service(staging)
        self.assertTrue(os.path.isfile(os.path.join(source, "AnyKirSvc.exe")))
        self.assertTrue(os.path.isfile(os.path.join(source, "_internal", "PIL", "_imaging.pyd")))
        self.assertFalse(os.path.exists(os.path.join(source, "_internal", "svc")))


class AdminVersionTests(unittest.TestCase):

    def test_server_keeps_only_a_clean_version(self):
        import hub
        self.assertEqual(hub._service_field({"ver": "1.1.39", "state": "running"}), {"ver": "1.1.39", "state": "running"})
        self.assertEqual(hub._service_field({"ver": "<script>", "state": "stopped"}), {"ver": "", "state": "stopped"})
        self.assertEqual(hub._service_field({}), {})
        self.assertIsNone(hub._service_field(None))

    def test_column_text(self):
        import server
        self.assertEqual(server.service_text({"svc": {"ver": "1.1.39", "state": "running"}}), "v1.1.39")
        self.assertEqual(server.service_text({"svc": {"ver": "1.1.38", "state": "stopped"}}), "v1.1.38, stopped")
        self.assertEqual(server.service_text({"svc": {}}), "Not installed")
        self.assertEqual(server.service_text({}), "Unknown")
        self.assertEqual(server.service_text({"os": "Android", "svc": None}), "-")


class AllowListTests(unittest.TestCase):

    def test_clean_list(self):
        from common import clean_allow_list
        self.assertEqual(clean_allow_list("192.168.1.20, 10.0.0.0/8; nope\n192.168.1.20, 192.168.5.7/24"),
                         (["192.168.1.20", "10.0.0.0/8", "192.168.5.0/24"], ["nope"]))
        self.assertEqual(clean_allow_list(["  ", "1.2.3.4/32"]), (["1.2.3.4"], []))

    def test_who_may_connect(self):
        from common import ip_allowed
        listed = {"allow_from_mode": "list", "allow_from": ["192.168.1.20", "10.0.0.0/8"]}
        self.assertTrue(ip_allowed({}, "8.8.8.8"))
        self.assertTrue(ip_allowed({"allow_from_mode": "all", "allow_from": ["1.1.1.1"]}, "8.8.8.8"))
        self.assertTrue(ip_allowed(listed, "192.168.1.20"))
        self.assertTrue(ip_allowed(listed, "10.200.3.4"))
        self.assertTrue(ip_allowed(listed, "::ffff:192.168.1.20"))
        for ip in ("192.168.1.21", "127.0.0.1", "", None, "hub", "AnyKir Server"):
            self.assertFalse(ip_allowed(listed, ip), ip)

    def test_refused_before_the_password(self):
        import host
        svc = object.__new__(host.HostService)
        svc.cfg = {"allow_from_mode": "list", "allow_from": ["192.168.1.20"]}
        sock = Mock()
        with patch.object(host, "server_handshake") as handshake:
            svc.serve(sock, "192.168.1.99")
            svc.serve(Mock(), "AnyKir Server", "")
        handshake.assert_not_called()
        sock.close.assert_called_once()

    def test_admin_service_follows_the_choice(self):
        import json
        import os
        import tempfile
        import windows_service
        path = os.path.join(tempfile.mkdtemp(), "host.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"allow_from_mode": "list", "allow_from": ["192.168.1.20", "junk", 5]}, f)
        got = windows_service.read_app_config(path)
        self.assertEqual(got["allow_from_mode"], "list")
        self.assertEqual(got["allow_from"], ["192.168.1.20"])


class ShareOnAddressTests(unittest.TestCase):

    def test_listener_address(self):
        import host
        svc = object.__new__(host.HostService)
        svc.cfg = {"address": "192.168.1.20"}
        self.assertEqual(svc.bind_ip(), "")
        svc.cfg["only_address"] = True
        self.assertEqual(svc.bind_ip(), "192.168.1.20")
        self.assertEqual(svc.shared_ips(), ["192.168.1.20"])
        svc.cfg["address"] = None
        self.assertEqual(svc.bind_ip(), "")

    def test_admin_service_follows(self):
        import json
        import os
        import tempfile
        import windows_service
        path = os.path.join(tempfile.mkdtemp(), "host.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"address": "192.168.1.20", "only_address": True}, f)
        got = windows_service.read_app_config(path)
        self.assertEqual((got["address"], got["only_address"]), ("192.168.1.20", True))
        self.assertIn("only_address", windows_service.NETWORK_FIELDS)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"address": "not an ip", "only_address": "yes"}, f)
        self.assertEqual(windows_service.read_app_config(path), {})


class ServerAddressTests(unittest.TestCase):

    def test_server_tells_its_address_on_the_same_network(self):
        import hub
        srv = hub.Hub(0)
        srv.port = 0
        self.assertTrue(srv.start())
        port = srv.srv.getsockname()[1]
        srv._addresses = (["192.168.210.112", "100.123.9.43"], 1e18)
        try:
            self.assertEqual(hub.server_address_for("127.0.0.1", port, "100.64.1.2"), ("100.123.9.43", port))
            self.assertEqual(hub.server_address_for("127.0.0.1", port, "192.168.210.40"), ("192.168.210.112", port))
            self.assertIsNone(hub.server_address_for("127.0.0.1", port, "10.0.0.5"))
        finally:
            srv.stop()


class ViewerScalingTests(unittest.TestCase):

    def test_piecewise_matches_whole(self):
        import random
        import numpy as np
        from PIL import Image
        import viewer
        s = viewer.ViewerSession()
        s.fb = Image.new("RGB", (1920, 1080))
        for vs in ((1280, 720), (1858, 1045), (1920, 1080)):
            s.view_size, s.view_box = vs, None
            s.view = s.fb.resize(vs, Image.BILINEAR)
            rnd = random.Random(7)
            for _ in range(120):
                w, h = rnd.randint(8, 600), rnd.randint(8, 400)
                x, y = rnd.randint(0, 1920 - w), rnd.randint(0, 1080 - h)
                tile = Image.new("RGB", (w, h), tuple(rnd.randint(0, 255) for _ in range(3)))
                with s.fb_lock:
                    s.fb.paste(tile, (x, y))
                    job = s._view_job(tile, x, y, w, h)
                if job:
                    s._scale_piece(*job)
            diff = np.abs(np.asarray(s.fb.resize(vs, Image.BILINEAR), np.int16) - np.asarray(s.view, np.int16))
            self.assertLessEqual(int(diff.max()), 2, vs)
            self.assertIsNotNone(s.view_box)


class ServerResponseTimeTests(unittest.TestCase):

    def test_server_measures_response_time(self):
        import time
        import hub
        srv = hub.Hub(0)
        srv.port = 0
        self.assertTrue(srv.start())
        port = srv.srv.getsockname()[1]
        alive = {"on": True}
        hub.call_home(f"127.0.0.1:{port}", lambda: {"id": "123456789", "name": "pc", "port": 7001},
                      lambda *a: None, lambda: alive["on"])
        try:
            for _ in range(50):
                online = srv.online()
                if online and online[0].get("rtt") is not None:
                    break
                time.sleep(0.1)
            self.assertIsNotNone(srv.online()[0].get("rtt"))
            self.assertIn("rtt", hub.fetch_online(f"127.0.0.1:{port}")[0])
        finally:
            alive["on"] = False
            srv.stop()


class ViewerServersTests(unittest.TestCase):

    def test_falls_back_and_remembers(self):
        import hub
        srv = hub.Hub(0)
        srv.port = 0
        self.assertTrue(srv.start())
        port = srv.srv.getsockname()[1]
        try:
            office = "192.0.2.1:7000"
            reachable = f"127.0.0.1:{port}"
            servers = [office, reachable]
            used, hosts = hub.fetch_online_any(servers, timeout=0.5)
            self.assertEqual((used, hosts), (reachable, []))
            t = __import__("time").perf_counter()
            used, _ = hub.fetch_online_any(servers, timeout=0.5)
            self.assertEqual(used, reachable)
            self.assertLess(__import__("time").perf_counter() - t, 0.4, "the one that answered is asked first")
        finally:
            srv.stop()

    def test_built_in_tailscale_address(self):
        import hub
        self.assertEqual(hub.viewer_servers("192.168.210.112"), ["192.168.210.112", "100.123.9.43"])
        self.assertEqual(hub.viewer_servers(""), hub.VIEWER_SERVERS)
