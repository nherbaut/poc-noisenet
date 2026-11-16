import importlib.util
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("bind_ip", ROOT / "scripts" / "bind_ip.py")
bind_ip = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bind_ip)


class Terminal(io.StringIO):
    def isatty(self):
        return True


class BindIPTests(unittest.TestCase):
    def test_dotenv_quotes_comments_and_last_assignment(self):
        text = 'BIND_IP=127.0.0.1\nexport BIND_IP = "192.168.1.20" # Wi-Fi\n'
        self.assertEqual(bind_ip.read_bind_ip(text), "192.168.1.20")
        self.assertEqual(bind_ip.read_bind_ip("BIND_IP='127.0.0.1'\n"), "127.0.0.1")
        self.assertEqual(bind_ip.read_bind_ip("BIND_IP= # select interactively\n"), "")

    def test_invalid_or_nonconcrete_addresses_are_rejected(self):
        for address in ("not-an-ip", "::1", "0.0.0.0", "224.0.0.1", "255.255.255.255", '"127.0.0.1" trailing'):
            with self.subTest(address=address), self.assertRaises(ValueError):
                bind_ip.read_bind_ip(f"BIND_IP={address}\n")

    def test_existing_config_does_not_prompt_or_enumerate_interfaces(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            original = "OTHER=keep-me\nBIND_IP=192.168.1.20\n"
            path.write_text(original)
            with patch.object(bind_ip, "local_addresses", side_effect=AssertionError("unexpected enumeration")):
                self.assertEqual(bind_ip.ensure_bind_ip(path, io.StringIO(), io.StringIO()), "192.168.1.20")
            self.assertEqual(path.read_text(), original)

    def test_missing_config_fails_without_terminal_and_does_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            with self.assertRaisesRegex(ValueError, "make configure"):
                bind_ip.ensure_bind_ip(path, io.StringIO(), io.StringIO())
            self.assertFalse(path.exists())

    def test_selection_retries_and_preserves_other_dotenv_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("# settings\nOTHER=keep-me\nBIND_IP=\nexport BIND_IP='' # choose\n")
            stderr = io.StringIO()
            with patch.object(bind_ip, "local_addresses", return_value=[("127.0.0.1", "lo"), ("192.168.1.20", "wlan0")]):
                address = bind_ip.ensure_bind_ip(path, Terminal("bad\n0\n3\n2\n"), stderr)
            self.assertEqual(address, "192.168.1.20")
            self.assertIn("192.168.1.20 (wlan0)", stderr.getvalue())
            self.assertIn("OTHER=keep-me", path.read_text())
            self.assertEqual(bind_ip.read_bind_ip(path.read_text()), address)
            self.assertNotIn("export BIND_IP", path.read_text())

    def test_cancel_does_not_change_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("OTHER=keep-me")
            with patch.object(bind_ip, "local_addresses", return_value=[("127.0.0.1", "lo")]):
                with self.assertRaisesRegex(ValueError, "No IP selected"):
                    bind_ip.ensure_bind_ip(path, Terminal(""), io.StringIO())
            self.assertEqual(path.read_text(), "OTHER=keep-me")

    def test_ip_enumeration_deduplicates_interfaces(self):
        result = subprocess.CompletedProcess([], 0, stdout='[{"ifname":"eth0","addr_info":[{"family":"inet","local":"192.168.1.20"},{"family":"inet","local":"192.168.1.20"}]}]')
        with patch.object(bind_ip.subprocess, "run", return_value=result):
            self.assertEqual(bind_ip.local_addresses(), [("192.168.1.20", "eth0"), ("127.0.0.1", "loopback; local access only")])

    def test_make_passes_saved_ip_to_compose_and_prints_matching_topology(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "scripts" / "bind_ip.py").write_text((ROOT / "scripts" / "bind_ip.py").read_text())
            (root / "Makefile").write_text((ROOT / "Makefile").read_text())
            (root / ".env").write_text("BIND_IP=192.168.1.20\n")
            (root / "compose.sh").write_text('printf "compose-bind=%s args=%s\\n" "$BIND_IP" "$*"\n')
            result = subprocess.run(["make", "--no-print-directory", "up", "COMPOSE=sh ./compose.sh"], cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("compose-bind=192.168.1.20 args=up --build -d", result.stdout)
            self.assertIn("Viewer URL: http://192.168.1.20:8080", result.stdout)
            self.assertNotIn("@BIND_IP@", result.stdout)
            (root / "compose.sh").write_text("exit 42\n")
            result = subprocess.run(["make", "--no-print-directory", "up", "COMPOSE=sh ./compose.sh"], cwd=root, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("NoiseNet MQTT topology", result.stdout)


if __name__ == "__main__":
    unittest.main()
