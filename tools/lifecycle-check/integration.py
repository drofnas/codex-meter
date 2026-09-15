#!/usr/bin/env python3
"""Isolated real launchd/Docker lifecycle check; all test files stay in artifacts."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import meter


def wait(check, seconds=80):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        result = check()
        if result:
            return result
        time.sleep(1)
    raise RuntimeError('Timed out waiting for lifecycle evidence')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--port', type=int, default=18088)
    args = p.parse_args()
    out = args.output.resolve()
    if not out.is_relative_to(ROOT / 'artifacts') or out.exists():
        p.error('Use a new output directory under repository artifacts')
    out.mkdir(parents=True, mode=0o700)
    inst = meter.Installation(out / 'service')
    agents = out / 'LaunchAgents'
    events = []

    def event(name):
        events.append({'event': name, 'at': int(time.time())})
        (out / 'events.json').write_text(json.dumps(events, indent=2) + '\n')
        print(name, flush=True)

    def cli(action):
        cmd = [sys.executable, str(ROOT / 'scripts/meter.py'), action, '--directory', str(inst.directory),
               '--launch-agents-dir', str(agents)]
        if action == 'configure':
            cmd += ['--auth-file', str(out / 'absent-native/auth.json'), '--port', str(args.port)]
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=360)
        # CLI output is sanitized; never persist configuration or HTTP payloads.
        (out / (action + '.log')).write_text(r.stdout + r.stderr)
        if r.returncode:
            raise RuntimeError(f'{action} failed; see its sanitized log')

    def job_pid():
        r = subprocess.run(['launchctl', 'print', inst.target('collector')], capture_output=True, text=True)
        match = re.search(r'^\s*pid = (\d+)$', r.stdout, re.M)
        return int(match[1]) if match else None

    def reading():
        request = urllib.request.Request(f'http://127.0.0.1:{args.port}/v1/usage', headers={'Authorization': 'Bearer ' + token})
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return json.load(response)
        except (OSError, ValueError):
            return None

    try:
        cli('configure')
        config = inst.config.read_text().replace('METER_COLLECTION_SECONDS=60', 'METER_COLLECTION_SECONDS=10').replace('METER_STALE_SECONDS=180', 'METER_STALE_SECONDS=30')
        inst.config.write_text(config)
        token = dict(line.split('=', 1) for line in config.splitlines())['METER_API_TOKEN']
        now = int(time.time())
        key = bytes(range(32))
        meter.write(inst.directory / 'state/installation.key', key)
        legacy = {'version': 1, 'key_id': hashlib.sha256(key).hexdigest(), 'published_at': now - 120,
                  'observation': {'scope': 'a' * 64, 'used_percent': 20, 'reset_at': now + 432000,
                                  'observed_at': now - 120, 'resets_available': None}}
        meter.write(inst.directory / 'state/observation.json', json.dumps(legacy).encode())
        (agents).mkdir()
        sentinel = agents / 'unrelated.plist'; sentinel.write_bytes(b'leave intact')
        cli('doctor'); cli('install'); cli('start'); cli('start')
        first_pid = wait(job_pid)
        first = wait(reading)
        assert first['remaining_percent'] == 80 and first['status'] == 'stale', first.keys()
        state = json.loads((inst.directory / 'state/observation.json').read_text())
        assert state['version'] == 2 and state['observation'] == legacy['observation']
        history = state['history']
        assert job_pid() == first_pid
        duplicate = subprocess.run([str(inst.directory / 'meter-collector'), '--env-file', str(inst.config), '--once'], capture_output=True, text=True, timeout=10)
        assert duplicate.returncode == 1 and duplicate.stderr == 'collector_already_running\n'
        event('start_twice_one_collector_and_retained_active_cycle')
        # Warm no-change reconciliation baseline; time reports the maximum RSS
        # of an individual process, not simultaneous aggregate memory.
        measurements = []
        for _ in range(5):
            r = subprocess.run(['/usr/bin/time', '-l', sys.executable, str(ROOT / 'scripts/meter.py'),
                                '_api', '--directory', str(inst.directory)], capture_output=True, text=True, timeout=60)
            if r.returncode:
                raise RuntimeError('Reconciliation baseline failed; retry after scheduled job completes')
            timing = re.search(r'([0-9.]+) real\s+([0-9.]+) user\s+([0-9.]+) sys', r.stderr)
            rss = re.search(r'(\d+)\s+maximum resident set size', r.stderr)
            assert timing and rss
            measurements.append({'wall_seconds': float(timing[1]), 'user_seconds': float(timing[2]),
                                 'system_seconds': float(timing[3]), 'max_process_rss_bytes': int(rss[1])})
        (out / 'reconcile-baseline.json').write_text(json.dumps(measurements, indent=2) + '\n')
        cli('status')
        cli('stop')
        assert not job_pid()
        cli('start'); wait(job_pid); wait(reading)
        assert (inst.directory / 'state/installation.key').read_bytes() == key
        assert json.loads((inst.directory / 'state/observation.json').read_text())['history'] == history
        event('stop_start_preserves_key_observation_and_history')
        crash_pid = job_pid()
        subprocess.run(['launchctl', 'kill', 'SIGKILL', inst.target('collector')], check=True)
        wait(lambda: (pid := job_pid()) and pid != crash_pid)
        assert json.loads((inst.directory / 'state/observation.json').read_text())['history'] == history
        event('collector_crash_restarted_automatically')
        subprocess.run(['docker', 'rm', '-f', inst.identity + '-api-1'], check=True, stdout=subprocess.DEVNULL)
        assert reading() is None
        wait(reading, seconds=90)
        event('removed_api_restored_by_calendar_job')
        cli('uninstall')
        assert sentinel.read_bytes() == b'leave intact'
        assert not inst.record.exists()
        assert (inst.directory / 'state/installation.key').read_bytes() == key
        assert json.loads((inst.directory / 'state/observation.json').read_text())['history'] == history
        cli('install'); cli('start'); wait(reading)
        assert json.loads((inst.directory / 'state/observation.json').read_text())['history'] == history
        event('uninstall_reinstall_preserves_history_and_unrelated_artifacts')
        cli('uninstall')
        assert not (out / 'absent-native').exists()
        assert not job_pid()
        event('cleanup_complete_native_credentials_never_created_or_opened')
    finally:
        if inst.record.exists():
            cli('uninstall')


if __name__ == '__main__':
    main()
