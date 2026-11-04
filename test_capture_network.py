import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image
import numpy as np

import desktop_capture
import network_info
import windows_service


class CaptureTests(unittest.TestCase):
    def test_capture_negative_virtual_origin_and_monitor_crop(self):
        displays = [{"left": -4, "top": 0, "width": 8, "height": 3},
                    {"left": 0, "top": 0, "width": 4, "height": 3},
                    {"left": -4, "top": 0, "width": 4, "height": 3}]
        image = Image.new("RGB", (8, 3), "red")
        image.paste(Image.new("RGB", (4, 3), "blue"), (4, 0))
        with patch.object(desktop_capture, "monitors", return_value=displays), \
                patch.object(desktop_capture.ImageGrab, "grab", return_value=image):
            capture = desktop_capture.DesktopCapture()
            pixels, monitor, count = capture.grab(1)
            self.assertEqual(pixels.shape, (3, 4))
            self.assertEqual(monitor["left"], 0)
            self.assertEqual(count, 2)
            self.assertEqual(int(pixels[0, 0]) & 0xFFFFFF, 0x0000FF)
            all_pixels, _, _ = capture.grab(0)
            self.assertEqual(all_pixels.shape, (3, 8))

    def test_display_change_during_capture_is_retried(self):
        with patch.object(desktop_capture, "monitors", return_value=[{"left": 0, "top": 0, "width": 4, "height": 3}]), \
                patch.object(desktop_capture.ImageGrab, "grab", return_value=Image.new("RGB", (2, 2))):
            with self.assertRaisesRegex(OSError, "geometry changed"):
                desktop_capture.DesktopCapture().grab(0)

    def test_capture_error_closes_old_source_and_recovers_with_new_source(self):
        import host
        stop = threading.Event()
        session = object.__new__(host.HostSession)
        session.stop, session.cfg_lock = stop, threading.Lock()
        session.cfg = {"monitor": 1, "fps": 60}
        session.frame_cv = threading.Condition()
        session.frame = None
        session.force_full = threading.Event()
        session.active_until = 0
        session.ch = Mock()
        old, new = Mock(name="old"), Mock(name="new")
        old.grab.side_effect = OSError("desktop changed")
        old.close.side_effect = OSError("old DC already invalid")
        new.name = "win32"
        def recovered(index):
            stop.set()
            return np.zeros((2, 3), dtype=np.uint32), {"width": 3, "height": 2}, 1
        new.grab.side_effect = recovered
        binding = Mock()
        binding.sync.return_value = False
        with patch.object(host, "Grabber", side_effect=[old, new]) as factory, \
                patch.object(host.windows_desktop, "DesktopBinding", return_value=binding):
            session._grab_loop()
        self.assertEqual(factory.call_count, 2)
        old.close.assert_called_once()
        self.assertIsNotNone(session.frame)
        statuses = [call.args[0] for call in session.ch.send_json.call_args_list]
        self.assertFalse(statuses[0]["ok"])
        self.assertTrue(statuses[-1]["ok"])

    def test_console_agent_survives_sign_out_without_username(self):
        import win32ts
        with patch.object(win32ts, "WTSGetActiveConsoleSessionId", return_value=1), \
                patch.object(win32ts, "WTSQuerySessionInformation") as username:
            self.assertEqual(windows_service.console_session(), 1)
            username.assert_not_called()
        for session in (0, 0xFFFFFFFF):
            with patch.object(win32ts, "WTSGetActiveConsoleSessionId", return_value=session):
                self.assertIsNone(windows_service.console_session())


class NetworkTests(unittest.TestCase):
    def test_tailscale_range_and_invalid_values(self):
        for address in ("100.64.0.1", "100.123.9.43", "100.127.255.254:7001"):
            self.assertTrue(network_info.is_tailscale(address))
        for address in ("100.128.0.1", "192.168.1.2", "100.bad.1.2", "", "example.com"):
            self.assertFalse(network_info.is_tailscale(address))

    def test_failed_cli_candidate_does_not_stop_discovery(self):
        with patch.object(network_info.socket, "getaddrinfo", return_value=[(None, None, None, None, ("192.168.1.2", 0))]), \
                patch.object(network_info.subprocess, "run", side_effect=[
                    SimpleNamespace(returncode=1, stdout=""),
                    SimpleNamespace(returncode=0, stdout="100.123.9.43\n")]):
            with patch.dict(network_info.os.environ, {"ProgramW6432": "C:/native", "ProgramFiles": "C:/programs"}):
                self.assertEqual(network_info.local_ips()[0], "100.123.9.43")

    def test_no_address_is_not_reported_as_vpn_disconnected(self):
        note, _ = network_info.status_text([])
        self.assertIn("not detected", note)
        self.assertNotIn("disconnected", note.lower())
        self.assertIn("can still work", note)


if __name__ == "__main__":
    unittest.main()
