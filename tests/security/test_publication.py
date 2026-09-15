"""Exercise publication checks in disposable repositories with synthetic secrets."""
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/check_secrets.py"
CONFIG = SCRIPT.parents[1] / ".gitleaks.toml"
CHECKSUMS = SCRIPT.parents[1] / "tests/display/landscape.sha256.json"


class PublicationChecks(unittest.TestCase):
    def setUp(self):
        if not shutil.which("gitleaks"):
            self.fail("Install Gitleaks to run publication tests")
        self.temp = tempfile.TemporaryDirectory(prefix="meter-publication-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "scripts").mkdir()
        shutil.copyfile(SCRIPT, self.root / "scripts/check_secrets.py")
        shutil.copyfile(CONFIG, self.root / ".gitleaks.toml")
        (self.root / ".gitignore").write_text(".env\nartifacts/\n")
        self.git("init", "--quiet", "--initial-branch=main")

    def git(self, *args):
        return subprocess.run(["git", "-c", "user.name=Fixture", "-c",
                               "user.email=fixture@example.com", *args], cwd=self.root,
                              check=True, capture_output=True, text=True)

    def scan(self):
        return subprocess.run([sys.executable, "scripts/check_secrets.py"], cwd=self.root,
                              capture_output=True, text=True, timeout=30)

    def test_ignored_private_files_are_not_scanned(self):
        (self.root / ".env").write_text("API_KEY=" + secrets.token_hex(32))
        (self.root / "artifacts").mkdir()
        (self.root / "artifacts/firmware.bin").write_bytes(os.urandom(32))
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Git history empty", result.stdout)

    def test_new_secret_is_rejected_and_redacted(self):
        canary = secrets.token_hex(32)
        (self.root / "config.txt").write_text("API_KEY=" + canary)
        result = self.scan()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn(canary, result.stdout + result.stderr)

    def test_personal_path_and_tracked_private_file_are_rejected(self):
        (self.root / "notes.md").write_text("/" + "Users" + "/synthetic-person/project")
        (self.root / ".env").write_text("local settings")
        self.git("add", "--force", ".env")
        result = self.scan()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("personal home-directory path", result.stdout)
        self.assertIn(".env: private", result.stdout)

    def test_deleted_secret_remains_detectable_in_history(self):
        canary = secrets.token_hex(32)
        path = self.root / "config.txt"
        path.write_text("API_KEY=" + canary)
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "Synthetic scanner fixture")
        path.unlink()
        result = self.scan()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn(canary, result.stdout + result.stderr)

    def write_checksums(self, values, relative="tests/display/landscape.sha256.json"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(values, indent=2) + "\n")
        return path

    def test_known_image_checksums_pass_in_candidate_and_history(self):
        path = self.write_checksums(json.loads(CHECKSUMS.read_text()))
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "Synthetic landscape checksums")
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # The historical values stay allowed even when the file is later removed.
        path.unlink()
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_other_secrets_in_checksum_file_are_rejected(self):
        original = json.loads(CHECKSUMS.read_text())
        for key in ("auth-failed", "api_key"):
            with self.subTest(key=key):
                canary = secrets.token_hex(32)
                self.write_checksums(dict(original, **{key: canary}))
                result = self.scan()
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertNotIn(canary, result.stdout + result.stderr)

    def test_checksum_exception_does_not_apply_to_other_files(self):
        original = json.loads(CHECKSUMS.read_text())
        self.write_checksums({"auth-failed": original["auth-failed"]}, "config.json")
        result = self.scan()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_checksum_exception_does_not_hide_an_appended_secret(self):
        original = json.loads(CHECKSUMS.read_text())
        path = self.write_checksums({"auth-failed": original["auth-failed"]})
        canary = secrets.token_hex(32)
        path.write_text(path.read_text().replace('\n}', ', "api_key": "' + canary + '"\n}'))
        result = self.scan()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn(canary, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
