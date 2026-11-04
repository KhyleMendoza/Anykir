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

    def test_service_clipboard_requests_refused(self):
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


class FollowAppTests(unittest.TestCase):

    def _service(self, **cfg):
        base = {"id": "123456789", "secret": "s" * 24, "password": "oldpassword", "relay": "", "relay_key": "",
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
        with patch("common.save_json"):
            windows_service.apply_app_config(svc, {"listen": 7005})
        svc.restart_network.assert_called_once()

    def test_no_change_does_nothing(self):
        import windows_service
        svc = self._service()
        with patch("common.save_json") as save:
            self.assertEqual(windows_service.apply_app_config(svc, {"password": "oldpassword"}), {})
        save.assert_not_called()

    def test_access_rights_and_bad_values_are_ignored(self):
        import windows_service
        path = self._app_file({"password": "short", "id": "12ab", "allow_files": True, "allow_update": True,
                               "allow_clipboard": True, "listen": "7001", "relay": "r:7000", "allow_input": "yes"})
        self.assertEqual(windows_service.read_app_config(path), {"relay": "r:7000"})

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
            self.assertEqual(windows_service.apply_app_config(svc, {"listen": 0, "relay": ""}), {})
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
