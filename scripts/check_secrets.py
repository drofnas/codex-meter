#!/usr/bin/env python3
"""Scan publishable files and Git history without reading ignored private files."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
HOME_PATH = re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+")
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
PRIVATE_DIRS = {".git", ".local", ".esphome", "artifacts", ".venv", "__pycache__"}
PRIVATE_SUFFIXES = {".bin", ".elf", ".map", ".log", ".jsonl", ".pem", ".key"}


def main():
    scanner = shutil.which("gitleaks")
    if not scanner:
        print("Install Gitleaks before running publication checks (see CONTRIBUTING.md).")
        return 2
    raw = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT)
    paths = sorted({Path(os.fsdecode(p)) for p in raw.split(b"\0") if p})
    problems = []
    count = 0
    with tempfile.TemporaryDirectory(prefix="meter-publication-") as directory:
        target = Path(directory)
        for relative in paths:
            path = ROOT / relative
            if not path.exists() and not path.is_symlink():
                continue  # A deleted tracked file is absent from the candidate.
            name = relative.name
            private = (any(part in PRIVATE_DIRS for part in relative.parts)
                       or relative.suffix in PRIVATE_SUFFIXES or name == "auth.json"
                       or name.endswith(".local.yaml")
                       or (name.startswith(".env") and name != ".env.example")
                       or (name.startswith("secrets") and name.endswith(".yaml")
                           and name != "secrets.example.yaml"))
            symlink = any((ROOT / parent).is_symlink()
                          for parent in (relative, *relative.parents))
            if private or symlink or not path.is_file():
                problems.append(f"{relative}: private or unsupported publication path")
                continue
            data = path.read_bytes()
            for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                if HOME_PATH.search(line):
                    problems.append(f"{relative}:{number}: personal home-directory path")
                if any(m[1].lower() not in ("example.com", "example.org", "example.net")
                       and m[1].lower() != "users.noreply.github.com"
                       for m in EMAIL.finditer(line)):
                    problems.append(f"{relative}:{number}: review email address before publishing")
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            count += 1
        for problem in problems:
            print(problem, flush=True)  # Locations only; never print matched values.
        result = subprocess.run([scanner, "dir", str(target), "--redact=100", "--no-banner",
                                 "--max-decode-depth=3", "--max-archive-depth=3"], cwd=ROOT)
    has_history = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=ROOT,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    history_result = 0
    if has_history:
        history_result = subprocess.run(
            [scanner, "git", ".", "--log-opts=--all --full-history", "--redact=100", "--no-banner"],
            cwd=ROOT).returncode
    if problems or result.returncode or history_result:
        return 1
    print(f"Publication scan passed: {count} files; Git history {'scanned' if has_history else 'empty'}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
