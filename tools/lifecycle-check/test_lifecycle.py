"""Safety and recovery tests without touching the user's credentials or services."""
import hashlib
import http.server
import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import meter


class Lifecycle(unittest.TestCase):
    def setUp(self):
        out = ROOT / 'artifacts/lifecycle-tests'
        out.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=out)
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.inst = meter.Installation(self.base / 'service')
        self.inst.directory.mkdir(mode=0o700)
        self.args = type('Args', (), {'bind_address': '127.0.0.1', 'port': 8080,
                                    'auth_file': str(self.base / 'native/auth.json')})()
        with contextlib.redirect_stdout(io.StringIO()):
            self.inst.configure(self.args)
        self.m = {'version': 1, 'root': str(ROOT), 'identity': self.inst.identity,
                  'agents': str(self.base / 'LaunchAgents'), 'docker': '/test/docker',
                  'python': sys.executable, 'path': '/usr/bin:/bin'}
        self.m['plists'] = {s: hashlib.sha256(self.inst.plist(self.m, s)).hexdigest() for s in ('collector', 'api')}
        meter.write(self.inst.record, json.dumps(self.m).encode())

    def test_private_defaults_and_literal_config(self):
        env, auth = self.inst.configuration()
        self.assertEqual(len(env['METER_API_TOKEN']), 64)
        self.assertEqual(self.inst.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(env['METER_DATA_DIR']).stat().st_mode & 0o777, 0o750)
        self.assertFalse(auth.exists())
        self.assertNotIn('METER_AUTH_FILE', env)
        with patch.dict(os.environ, {'METER_API_TOKEN': 'bad', 'METER_DATA_DIR': '/bad'}):
            self.assertEqual(self.inst.configuration()[0], env)
        with self.assertRaisesRegex(meter.Problem, 'already exists'):
            self.inst.configure(self.args)

    def test_permissions_symlinks_and_credential_overlap(self):
        data = self.inst.directory / 'data'
        data.chmod(0o777)
        with self.assertRaisesRegex(meter.Problem, 'permissions'):
            self.inst.configuration()
        data.chmod(0o700)
        with self.assertRaisesRegex(meter.Problem, 'group read/search'):
            self.inst.configuration()
        data.chmod(0o750)
        self.inst.config.chmod(0o644)
        with self.assertRaisesRegex(meter.Problem, 'mode 600'):
            self.inst.configuration()
        self.inst.config.chmod(0o600)
        before = self.inst.config.read_text()
        self.inst.config.write_text(before.replace(str(data), str(self.base / 'native')))
        with self.assertRaisesRegex(meter.Problem, 'overlap'):
            self.inst.configuration()
        self.inst.config.write_text(before)
        data.rmdir()
        data.symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(meter.Problem):
            self.inst.configuration()
        data.unlink()

    def test_job_recovery_and_secret_boundaries(self):
        env, _ = self.inst.configuration()
        collector = plistlib.loads(self.inst.plist(self.m, 'collector'))
        api = plistlib.loads(self.inst.plist(self.m, 'api'))
        self.assertTrue(collector['KeepAlive'])
        self.assertEqual(api['StartCalendarInterval'], [{'Minute': minute} for minute in range(60)])
        self.assertEqual(collector['StandardOutPath'], os.devnull)
        self.assertEqual(collector['StandardErrorPath'], os.devnull)
        self.assertEqual(collector['ThrottleInterval'], 30)
        for service in ('collector', 'api'):
            self.assertNotIn(env['METER_API_TOKEN'].encode(), self.inst.plist(self.m, service))
        with patch.object(self.inst, 'api_responding', return_value=False), patch.object(self.inst, 'docker'), patch.object(self.inst, 'compose') as compose:
            with patch.object(meter, 'run', return_value=subprocess.CompletedProcess([], 1, '')):
                self.inst.reconcile()
            compose.assert_called_once()
            self.assertEqual(compose.call_args.args[1], ['up', '--detach', '--no-build'])
            compose.reset_mock()
            with patch.object(meter, 'run', return_value=subprocess.CompletedProcess([], 0, 'true\n')):
                self.inst.reconcile()
            compose.assert_not_called()
        self.assertLess((self.inst.directory / 'api-status.json').stat().st_size, 128)

    def test_missing_docker_timeout_are_actionable(self):
        with patch.object(meter.subprocess, 'Popen', side_effect=FileNotFoundError):
            with self.assertRaisesRegex(meter.Problem, 'install'):
                meter.run(['docker'])
        process = Mock(pid=123)
        process.communicate.side_effect = [subprocess.TimeoutExpired(['docker'], 10), ('', None)]
        with patch.object(meter.subprocess, 'Popen', return_value=process), patch.object(meter.os, 'killpg') as kill:
            with self.assertRaisesRegex(meter.Problem, 'Docker Desktop'):
                meter.run(['docker'])
            kill.assert_called_once_with(123, meter.signal.SIGKILL)
        with patch.object(meter, 'run', return_value=subprocess.CompletedProcess([], 1, '')):
            with self.assertRaisesRegex(meter.Problem, 'start Docker Desktop'):
                self.inst.docker(self.m)

    def test_uninstall_preserves_history_and_unrelated_files(self):
        agents = Path(self.m['agents']); agents.mkdir()
        for s in ('collector', 'api'):
            (agents / (self.inst.label + '.' + s + '.plist')).write_bytes(self.inst.plist(self.m, s))
        keep = [self.inst.config, self.inst.directory / 'state/observation.json',
                self.inst.directory / 'unrelated', agents / 'unrelated.plist']
        for path in keep[1:]:
            path.write_bytes(b'preserve-exactly')
        before = {p: p.read_bytes() for p in keep}
        with patch.object(self.inst, 'stop'):
            self.inst.uninstall()
        self.assertEqual(before, {p: p.read_bytes() for p in keep})
        self.assertFalse(self.inst.record.exists())
        self.assertEqual(list(agents.iterdir()), [agents / 'unrelated.plist'])

    def test_uninstall_refuses_changed_plist_and_wrong_owner(self):
        agents = Path(self.m['agents']); agents.mkdir()
        foreign = agents / (self.inst.label + '.collector.plist')
        foreign.write_bytes(b'not our job')
        with patch.object(self.inst, 'stop') as stop:
            with self.assertRaisesRegex(meter.Problem, 'ownership'):
                self.inst.uninstall()
            stop.assert_not_called()
        self.assertEqual(foreign.read_bytes(), b'not our job')
        self.m['root'] = '/different/checkout'
        meter.write(self.inst.record, json.dumps(self.m).encode())
        with self.assertRaisesRegex(meter.Problem, 'ownership'):
            self.inst.manifest()

    def test_stop_disables_jobs_even_without_docker(self):
        commands = []
        def command(argv, **kwargs):
            commands.append(argv)
            return subprocess.CompletedProcess(argv, 0, '')
        with patch.object(meter, 'run', side_effect=command), patch.object(self.inst, 'docker', side_effect=meter.Problem('Docker unavailable')):
            with self.assertRaisesRegex(meter.Problem, 'Docker unavailable'):
                self.inst.stop()
        self.assertEqual(sum(c[1] == 'disable' for c in commands), 2)
        self.assertEqual(sum(c[1] == 'bootout' for c in commands), 2)
        self.assertTrue(self.inst.record.exists())

    def test_owned_plists_survive_generator_upgrade(self):
        agents = Path(self.m['agents']); agents.mkdir()
        for service in ('collector', 'api'):
            (agents / (self.inst.label + '.' + service + '.plist')).write_bytes(self.inst.plist(self.m, service))
        with patch.object(self.inst, 'plist', return_value=b'new generator'), patch.object(self.inst, 'stop'):
            self.inst.uninstall()
        self.assertFalse(list(agents.iterdir()))

    def test_failed_reconciliation_status_is_bounded(self):
        with patch.object(self.inst, 'api_responding', return_value=False), patch.object(self.inst, 'docker', side_effect=meter.Problem('x' * 10000)):
            with self.assertRaises(meter.Problem):
                self.inst.reconcile()
        record = self.inst.directory / 'api-status.json'
        self.assertLess(record.stat().st_size, 1024)
        self.assertEqual(json.loads(record.read_text())['status'], 'waiting')

    def test_authenticated_api_check_avoids_children_and_rejects_bad_responses(self):
        env, _ = self.inst.configuration()
        responses = [(200, b'{"version":2,"status":"ok"}'),
                     (200, b'{"version":2,"status":"stale"}'),
                     (200, b'{"version":2,"status":"unavailable"}'),
                     (401, b'{"version":2,"status":"ok"}'),
                     (503, b'{"version":2,"error":"snapshot_invalid"}'),
                     (200, b' ' * 4097), (200, b'not JSON'),
                     (200, b'{"version":true,"status":"ok"}'), (200, b'[]')]
        requests = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, self.headers.get('Authorization')))
                status, body = responses.pop(0)
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args): pass
        server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        env['METER_API_PORT'] = str(server.server_port)
        try:
            with patch.object(self.inst, 'configuration', return_value=(env, None)), patch.object(meter, 'run', side_effect=AssertionError('healthy check spawned a child')):
                self.inst.reconcile()
            self.assertEqual(json.loads((self.inst.directory / 'api-status.json').read_text())['status'], 'running')
            self.assertTrue(self.inst.api_responding(env))
            self.assertTrue(self.inst.api_responding(env))
            for _ in range(6): self.assertFalse(self.inst.api_responding(env))
            self.assertEqual(requests, [('/v2/usage', 'Bearer ' + env['METER_API_TOKEN'])] * 9)
        finally:
            server.shutdown(); server.server_close(); worker.join()
        self.assertFalse(self.inst.api_responding(env))

    def test_lifecycle_commands_serialize(self):
        with self.inst.lock():
            with self.assertRaisesRegex(meter.Problem, 'Another lifecycle'):
                with self.inst.lock():
                    self.fail('second lock acquired')
        with self.inst.lock():
            pass


if __name__ == '__main__':
    unittest.main()
