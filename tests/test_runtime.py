import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.request import urlopen
from unittest.mock import patch

from epics_field_client.runtime import open_terminal, system_environment, open_browser


class DesktopLifecycleTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux PTY lifecycle")
    def test_closing_real_terminal_pty_stops_cleanly(self):
        import fcntl
        import pty
        import re
        import select
        import termios

        master, slave = pty.openpty()
        def terminal_session():
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryFile() as errors:
            process = subprocess.Popen([sys.executable, "-m", "epics_field_client", "--no-browser", "--port", "0", "--data-dir", folder],
                                       stdin=slave, stdout=slave, stderr=errors, preexec_fn=terminal_session)
            os.close(slave)
            try:
                output = b""
                until = time.monotonic() + 10
                while time.monotonic() < until and b"http://127.0.0.1:" not in output:
                    if select.select([master], [], [], .2)[0]:
                        output += os.read(master, 4096)
                url = re.search(rb"http://127\.0\.0\.1:\d+", output)[0].decode()
                with urlopen(url, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                os.close(master)
                master = None
                result = process.wait(timeout=8)
                errors.seek(0)
                self.assertEqual(result, 0, errors.read().decode())
                with self.assertRaises(OSError):
                    urlopen(url, timeout=1)
            finally:
                if master is not None:
                    os.close(master)
                if process.poll() is None:
                    process.kill()
                process.wait()

    def test_terminal_relaunch_preserves_virtual_environment_and_arguments(self):
        interpreter = os.path.abspath(sys.executable)  # Keep the real venv symlink.
        with patch("epics_field_client.runtime.shutil.which", return_value="/usr/bin/gnome-terminal"), patch("epics_field_client.runtime.subprocess.call", return_value=0) as run:
            open_terminal(["--data-dir", "/tmp/field data"])
        self.assertEqual(run.call_args.args[0], ["/usr/bin/gnome-terminal", "--wait", "--", interpreter, "-m", "epics_field_client", "--data-dir", "/tmp/field data"])

    def test_frozen_system_launch_restores_host_libraries_without_changing_worker_env(self):
        with patch("epics_field_client.runtime.sys.frozen", True, create=True), patch("epics_field_client.runtime.sys.platform", "linux"), patch.dict(os.environ, {"LD_LIBRARY_PATH": "/private/app-libs", "LD_LIBRARY_PATH_ORIG": "/host/libs"}):
            self.assertEqual(system_environment()["LD_LIBRARY_PATH"], "/host/libs")
            self.assertEqual(os.environ["LD_LIBRARY_PATH"], "/private/app-libs")
            def browser(url):
                self.assertEqual(url, "http://127.0.0.1:8765")
                self.assertEqual(os.environ["LD_LIBRARY_PATH"], "/host/libs")
                return True
            with patch("epics_field_client.runtime.webbrowser.open", side_effect=browser):
                self.assertTrue(open_browser("http://127.0.0.1:8765"))
            self.assertEqual(os.environ["LD_LIBRARY_PATH"], "/private/app-libs")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux terminal hangup")
    def test_terminal_hangup_stops_http_server(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "console.log"
            with log.open("w") as output:
                process = subprocess.Popen(
                    [sys.executable, "-m", "epics_field_client", "--no-browser", "--port", "0", "--data-dir", folder],
                    stdout=output, stderr=output,
                )
                try:
                    for _ in range(100):
                        text = log.read_text()
                        if "http://127.0.0.1:" in text:
                            break
                        time.sleep(0.05)
                    url = text.split("http://127.0.0.1:", 1)[1].split()[0]
                    url = "http://127.0.0.1:" + url
                    with urlopen(url, timeout=2) as response:
                        self.assertEqual(response.status, 200)
                    process.send_signal(signal.SIGHUP)
                    self.assertEqual(process.wait(timeout=8), 0)
                    with self.assertRaises(OSError):
                        urlopen(url, timeout=1)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait()


if __name__ == "__main__":
    unittest.main()
