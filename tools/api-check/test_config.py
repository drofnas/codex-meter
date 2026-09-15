import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("api_launcher", ROOT / "scripts/api.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class Configuration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base / "data").mkdir()
        self.env = {"METER_DATA_DIR": "data", "METER_STATE_DIR": "state",
                    "METER_AUTH_FILE": "credentials/auth.json", "METER_TIMEZONE": "UTC",
                    "METER_API_TOKEN": "a" * 64}

    def load(self, argv=(), env=None):
        return launcher.load(launcher.parser().parse_args(["check", *argv]),
                             self.env if env is None else env, self.base)

    def test_safe_subset(self):
        value = self.load()
        self.assertEqual(value["METER_API_BIND_ADDRESS"], "127.0.0.1")
        self.assertEqual(value["METER_DATA_DIR"], str((self.base / "data").resolve()))
        self.assertNotIn("METER_AUTH_FILE", value)
        self.assertNotIn("METER_STATE_DIR", value)
        self.assertEqual(len(value), 11)

    def test_precedence_and_literal_file(self):
        nested = self.base / "nested"
        nested.mkdir()
        (nested / "data").mkdir()
        settings = nested / "settings"
        settings.write_text("\n".join(f"{k}={v}" for k, v in self.env.items()) + "\nMETER_API_PORT=9000\n")
        value = self.load(["--env-file", str(settings)], {})
        self.assertEqual(value["METER_API_PORT"], "9000")
        self.assertEqual(value["METER_DATA_DIR"], str((nested / "data").resolve()))
        self.assertEqual(self.load(["--env-file", str(settings)], {"METER_API_PORT": "9001"})["METER_API_PORT"], "9001")
        self.assertEqual(self.load(["--api-port", "9002"], {**self.env, "METER_API_PORT": "9001"})["METER_API_PORT"], "9002")
        for line in ("METER_API_TOKEN=$(touch unsafe)", "METER_API_PORT=9000\nMETER_API_PORT=9001", "METER_UNKNOWN=x", "broken"):
            settings.write_text(line)
            with self.assertRaises(ValueError):
                self.load(["--env-file", str(settings)], {})
        self.assertFalse((self.base / "unsafe").exists())

    def test_invalid_values(self):
        mutations = {"METER_API_BIND_ADDRESS": ("", "0.0.0.0", "::", "localhost"),
                     "METER_API_PORT": ("", "+8080", "80", "65536"),
                     "METER_API_TOKEN": ("", "A" * 64), "METER_TIMEZONE": ("Local", "Bogus/Zone"),
                     "METER_STALE_SECONDS": ("119", "3601"), "METER_COLLECTION_SECONDS": ("9", "301"),
                     "METER_SOURCE_TIMEOUT_SECONDS": ("0", "31"), "METER_DATA_DIR": ("", "missing", "state", "credentials/data")}
        for key, values in mutations.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises((ValueError, KeyError)):
                    self.load(env={**self.env, key: value})
        with self.assertRaises(ValueError):
            self.load(env={**self.env, "METER_UNKNOWN": "x"})
        with self.assertRaises(ValueError):
            self.load(["--api-token", ""])

    def test_symlink_boundary(self):
        (self.base / "credentials").mkdir()
        (self.base / "alias").symlink_to(self.base / "credentials", target_is_directory=True)
        with self.assertRaises(ValueError):
            self.load(["--data-dir", "alias"])
        (self.base / "dangling").symlink_to(self.base / "absent")
        with self.assertRaises(ValueError):
            self.load(["--data-dir", "dangling"])


if __name__ == "__main__":
    unittest.main()
