#!/usr/bin/env python3
"""Configure and manage the private macOS meter installation (Python 3.11+)."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import time

import api

ROOT = Path(__file__).resolve().parents[1]


class Problem(Exception):
    pass


def private_dir(path, mode=0o700):
    # Never repair permissions silently, and never follow an installation symlink.
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise Problem("Unsafe directory symlink; choose a real installation/data directory.")
    path.mkdir(parents=True, mode=mode, exist_ok=True)
    s = path.stat()
    if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) & ~mode:
        raise Problem(f"Directory permissions are unsafe; use owner-only state/service directories (700) and data permissions 750: {path}")
    if s.st_mode & 0o700 != 0o700:
        raise Problem(f"Directory needs owner read/write/search permissions: {path}")


def private_file(path):
    s = path.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077:
        raise Problem(f"Private file must be a regular file owned by you with mode 600: {path}")


def write(path, data, mode=0o600):
    temp = path.with_name('.' + path.name + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def child_env():
    return {k: v for k, v in os.environ.items() if not k.startswith(('METER_', 'API_', 'COMPOSE_'))}


def run(argv, *, env=None, timeout=30, input=None):
    try:
        process = subprocess.Popen(argv, cwd=ROOT, env=env or child_env(),
                                   stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                   text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
    except FileNotFoundError:
        raise Problem(f"Required executable unavailable: {Path(argv[0]).name}; install it and rerun install.") from None
    try:
        stdout, _ = process.communicate(input, timeout=timeout)
    except BaseException as exc:
        # Kill this command's process group, including a Compose/build child.
        # A timed-out reconciler must not leave a child that starts services later.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        if isinstance(exc, subprocess.TimeoutExpired):
            raise Problem("Command timed out; check Docker Desktop and service status, then retry.") from None
        raise
    return subprocess.CompletedProcess(argv, process.returncode, stdout)


class Installation:
    def __init__(self, directory):
        self.directory = Path(os.path.abspath(directory.expanduser()))
        self.config = self.directory / '.env'
        self.record = self.directory / 'installation.json'
        self.identity = 'codex-meter-' + hashlib.sha256(str(self.directory).encode()).hexdigest()[:12]
        self.label = 'local.' + self.identity
        self.domain = f'gui/{os.getuid()}'

    @contextmanager
    def lock(self):
        private_dir(self.directory)
        fd = os.open(self.directory / '.lifecycle.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise Problem('Lifecycle lock must be a regular file.')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Problem('Another lifecycle command is running; retry shortly.') from None
            yield
        finally:
            os.close(fd)

    def configure(self, args):
        if self.config.exists() or self.config.is_symlink():
            raise Problem('Configuration already exists; edit its literal values, then stop and start. No settings were overwritten.')
        address = ipaddress.IPv4Address(args.bind_address)
        if address.is_unspecified or address.is_multicast:
            raise Problem('Select one trusted LAN IPv4 address, or 127.0.0.1 for local checks.')
        values = dict(api.DEFAULTS)
        values.update(METER_AUTH_FILE=str(api.resolve(args.auth_file, ROOT)),
                      METER_DATA_DIR=str(self.directory / 'data'),
                      METER_STATE_DIR=str(self.directory / 'state'),
                      METER_API_BIND_ADDRESS=str(address), METER_API_PORT=str(args.port),
                      METER_API_TOKEN=secrets.token_hex(32))
        if any('\n' in v or '\r' in v for v in values.values()):
            raise Problem('Configuration paths cannot contain newlines.')
        write(self.config, ''.join(f'{k}={v}\n' for k, v in values.items()).encode())
        self.configuration()
        print(f'Private settings created: {self.config}\nCopy the dedicated API token into ignored secrets.yaml; it is never printed here.')

    def configuration(self):
        private_file(self.config)
        args = api.parser().parse_args(['check', '--env-file', str(self.config)])
        values, base = api.settings(args, {}, ROOT)
        data, state, auth = (api.resolve(values[k], base) for k in ('METER_DATA_DIR', 'METER_STATE_DIR', 'METER_AUTH_FILE'))
        paths = (data, state, auth.parent)
        if any(a.is_relative_to(b) or b.is_relative_to(a) for i, a in enumerate(paths) for b in paths[i+1:]):
            raise Problem('Data, state and native credential directories must not overlap.')
        # Installation artifacts must not be inside any of those directories.
        if any(self.directory.is_relative_to(p) for p in paths):
            raise Problem('Installation files must be outside data, state and native credential directories.')
        private_dir(data, 0o750)
        private_dir(state)
        env = api.load(args, {}, ROOT)
        if data.stat().st_gid != os.getgid() or data.stat().st_mode & 0o050 != 0o050:
            raise Problem(f'Docker needs data group read/search access and your primary group; check permissions 750 and group ownership: {data}')
        for p, mode in ((data / 'usage.json', 0o640), (data / 'history.json', 0o640), (state / 'observation.json', 0o600),
                        (state / 'installation.key', 0o600)):
            if p.exists() or p.is_symlink():
                s = p.lstat()
                if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o777 & ~mode:
                    raise Problem(f'Unsafe existing state/data file permissions; inspect before restarting: {p}')
        return env, auth

    def manifest(self):
        private_file(self.record)
        with self.record.open('rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise Problem('Installation record is too large; restore its original private copy.')
        m = json.loads(raw)
        if not isinstance(m, dict) or m.get('version') != 1 or m.get('root') != str(ROOT) or m.get('identity') != self.identity:
            raise Problem('Installation ownership does not match this checkout; use the original checkout to uninstall.')
        if (any(not isinstance(m.get(k), str) for k in ('agents', 'docker', 'python', 'path'))
                or any(not Path(m[k]).is_absolute() for k in ('agents', 'docker', 'python'))
                or not isinstance(m.get('plists'), dict)
                or any(not re.fullmatch(r'[a-f0-9]{64}', str(m['plists'].get(s, '')))
                       for s in ('collector', 'api'))):
            raise Problem('Invalid installation record; restore its original private copy before cleanup.')
        return m

    def owns_plist(self, m, service, path):
        expected = m.get('plists', {}).get(service)
        return (not path.is_symlink() and path.is_file() and expected is not None
                and hashlib.sha256(path.read_bytes()).hexdigest() == expected)

    def target(self, service):
        return self.domain + '/' + self.label + '.' + service

    def loaded(self, service):
        return run(['launchctl', 'print', self.target(service)]).returncode == 0

    def docker(self, m):
        # `info` inventories installed CLI plugins and spawns expensive children.
        # `version` checks the engine without that unrelated work each minute.
        if run([m['docker'], 'version', '--format', '{{.Server.Version}}'], timeout=10).returncode:
            raise Problem('Docker is unavailable; start Docker Desktop and wait for its engine, then retry. Automatic recovery will retry each minute while started.')

    def compose(self, m, action, *, env=None):
        base = [m['docker'], 'compose', '--project-name', self.identity, '--env-file', os.devnull]
        if action == ['down']:
            config = json.dumps({'services': {'api': {'image': 'codex-meter-api:local'}}})
            result = run([*base, '--file', '-', *action], input=config)
        else:
            result = run([*base, '--file', str(ROOT / 'compose.yaml'), *action],
                         env={**child_env(), **env}, timeout=300 if 'build' in action else 45)
        if result.returncode:
            raise Problem('Docker Compose failed; check engine availability, the configured LAN address/port, Docker file sharing, and rebuild with install if the image is missing.')

    def install(self, agents):
        env, _ = self.configuration()
        old = None
        if self.record.exists():
            old = self.manifest()
            if any(self.loaded(s) for s in ('collector', 'api')):
                raise Problem('Stop this installation before rebuilding it.')
            if str(agents) != old['agents']:
                raise Problem('Use the recorded LaunchAgents directory, or uninstall first.')
        docker = shutil.which('docker')
        if not docker:
            raise Problem('Docker CLI is missing; install and start Docker Desktop, then rerun install.')
        m = {'version': 1, 'root': str(ROOT), 'identity': self.identity,
             'agents': str(agents), 'docker': str(Path(docker).absolute()),
             'python': sys.executable, 'path': os.environ.get('PATH', '/usr/bin:/bin')}
        m['plists'] = {s: hashlib.sha256(self.plist(m, s)).hexdigest() for s in ('collector', 'api')}
        self.docker(m)
        if not shutil.which('go'):
            raise Problem('Go is missing; install Go 1.26.8 or a newer patched release to build the native collector.')
        # Refuse occupied names before building or overwriting anything.
        if any(p.is_symlink() for p in (agents, *agents.parents)):
            raise Problem('LaunchAgents directory must not be a symlink.')
        agents.mkdir(parents=True, exist_ok=True)
        if old is None and any((self.directory / n).exists() for n in ('meter-collector', 'meter-collector.new')):
            raise Problem('Unrecognized collector binary in the installation directory; choose a clean directory.')
        for service in ('collector', 'api'):
            p = agents / (self.label + '.' + service + '.plist')
            if p.exists() or p.is_symlink():
                if old is None or not self.owns_plist(old, service, p):
                    raise Problem('A LaunchAgent at this name is not owned by this installation; leave it intact.')
        cache = ROOT / 'artifacts/lifecycle-build'
        cache.mkdir(parents=True, exist_ok=True)
        result = run(['go', 'build', '-trimpath', '-ldflags=-s -w', '-o',
                      str(self.directory / 'meter-collector.new'), './cmd/meter-collector'],
                     env={**child_env(), 'GOCACHE': str(cache / 'go-cache'), 'GOTMPDIR': str(cache)}, timeout=300)
        if result.returncode:
            (self.directory / 'meter-collector.new').unlink(missing_ok=True)
            raise Problem('Native collector build failed; verify the Go toolchain and checkout.')
        try:
            self.compose(m, ['build'], env=env)
        except Problem:
            (self.directory / 'meter-collector.new').unlink(missing_ok=True)
            raise
        # Record before plist creation so an interrupted installation can be retried.
        write(self.record, (json.dumps(m, indent=2) + '\n').encode())
        os.replace(self.directory / 'meter-collector.new', self.directory / 'meter-collector')
        for service in ('collector', 'api'):
            if run(['launchctl', 'disable', self.target(service)]).returncode:
                raise Problem('Cannot access the macOS GUI launchd domain; run as the logged-in user, without sudo.')
            write(agents / (self.label + '.' + service + '.plist'), self.plist(m, service))
        print('Installed and stopped. Run start to enable collection and login startup.')

    def plist(self, m, service):
        value = {'Label': self.label + '.' + service, 'WorkingDirectory': str(ROOT),
                 'ProgramArguments': [m['python'], str(ROOT / 'scripts/meter.py'),
                                      '_' + service, '--directory', str(self.directory)],
                 'EnvironmentVariables': {'PATH': m['path'], 'HOME': str(Path.home())},
                 'RunAtLoad': True, 'ProcessType': 'Background', 'ThrottleInterval': 30,
                 'ExitTimeOut': 10, 'StandardOutPath': os.devnull, 'StandardErrorPath': os.devnull}
        if service == 'collector':
            value['KeepAlive'] = True
        else:
            # Every minute, coalescing missed firings on wake (launchd.plist(5)).
            value['StartCalendarInterval'] = [{'Minute': minute} for minute in range(60)]
        return plistlib.dumps(value, sort_keys=True)

    def start(self):
        self.configuration()
        m = self.manifest()
        self.docker(m)
        for service in ('collector', 'api'):
            p = Path(m['agents']) / (self.label + '.' + service + '.plist')
            if not self.owns_plist(m, service, p):
                raise Problem('Installed LaunchAgent differs from this installation; rerun install after stopping.')
            if run(['launchctl', 'enable', self.target(service)]).returncode:
                raise Problem('Cannot enable LaunchAgent; run as the logged-in macOS user.')
            if not self.loaded(service) and run(['launchctl', 'bootstrap', self.domain, str(p)]).returncode:
                raise Problem('LaunchAgent bootstrap failed; check macOS Login Items permissions and the GUI login session.')
        print('Started; collection and API reconciliation run automatically. Use status to check readiness.')

    def stop(self):
        m = self.manifest()
        for service in ('api', 'collector'):
            if run(['launchctl', 'disable', self.target(service)]).returncode:
                raise Problem('Cannot disable LaunchAgent; run as the logged-in macOS user.')
            if self.loaded(service) and run(['launchctl', 'bootout', self.target(service)]).returncode:
                raise Problem('Cannot stop LaunchAgent; retry stop before uninstalling.')
        # Retain the manifest if Docker is unavailable so cleanup can be retried.
        self.docker(m)
        self.compose(m, ['down'])
        print('Stopped; login startup disabled. History and private settings retained.')

    def uninstall(self):
        m = self.manifest()
        plists = [Path(m['agents']) / (self.label + '.' + s + '.plist') for s in ('collector', 'api')]
        for service, p in zip(('collector', 'api'), plists):
            if p.is_symlink() or (p.exists() and not self.owns_plist(m, service, p)):
                raise Problem('LaunchAgent ownership check failed; no installation files were removed.')
        self.stop()
        for p in plists:
            p.unlink(missing_ok=True)
        for name in ('meter-collector', 'meter-collector.new', 'api-status.json', 'installation.json'):
            (self.directory / name).unlink(missing_ok=True)
        print('Uninstalled owned jobs and binary. Configuration, cycle history, images and build cache retained; no credential files were changed.')

    def reconcile(self):
        # launchd serializes this job; the lifecycle lock also fences stop/uninstall.
        try:
            self.reconcile_api()
        except Problem as exc:
            write(self.directory / 'api-status.json', json.dumps({
                'checked_at': int(time.time()), 'status': 'waiting', 'diagnostic': str(exc)[:512]}).encode())
            raise

    def reconcile_api(self):
        m = self.manifest()
        env, _ = self.configuration()
        # A bounded authenticated request avoids spawning Docker CLI children
        # every healthy minute. Keep the existing engine/Compose recovery path
        # when the configured API cannot identify itself or is unreachable.
        if not self.api_responding(env):
            self.docker(m)
            state = run([m['docker'], 'inspect', '--format', '{{.State.Running}}', self.identity + '-api-1'])
            if state.returncode or state.stdout.strip() != 'true':
                self.compose(m, ['up', '--detach', '--no-build'], env=env)
        write(self.directory / 'api-status.json', json.dumps({'checked_at': int(time.time()), 'status': 'running'}).encode())

    def api_responding(self, env):
        connection = http.client.HTTPConnection(env['METER_API_BIND_ADDRESS'], int(env['METER_API_PORT']), timeout=2)
        try:
            connection.request('GET', '/v2/usage', headers={'Authorization': 'Bearer ' + env['METER_API_TOKEN']})
            response = connection.getresponse()
            raw = response.read(4097)
            if response.status != 200 or len(raw) > 4096:
                return False
            value = json.loads(raw)
            return (isinstance(value, dict) and type(value.get('version')) is int and value['version'] == 2
                    and value.get('status') in ('ok', 'stale', 'unavailable'))
        except (OSError, ValueError, http.client.HTTPException):
            return False
        finally:
            connection.close()

    def status(self):
        m = self.manifest()
        env, auth = self.configuration()
        status = {s: 'loaded' if self.loaded(s) else 'stopped' for s in ('collector', 'api')}
        for service in ('collector', 'api'):
            r = run(['launchctl', 'print', self.target(service)])
            match = re.search(r'^\s*last exit code = (\d+)$', r.stdout, re.M)
            if match:
                status[service + '_last_exit_code'] = int(match[1])
        record = self.directory / 'api-status.json'
        if record.exists():
            with record.open('rb') as stream:
                raw = stream.read(4097)
            if len(raw) <= 4096:
                status['api_check'] = json.loads(raw)
        try:
            self.docker(m)
            r = run([m['docker'], 'inspect', '--format', '{{.State.Running}}', self.identity + '-api-1'])
            status['container'] = 'running' if r.returncode == 0 and r.stdout.strip() == 'true' else 'stopped'
        except Problem as exc:
            status['container'] = str(exc)
        path = Path(env['METER_DATA_DIR']) / 'usage.json'
        if path.exists():
            with path.open('rb') as stream:
                raw = stream.read(4097)
            if len(raw) <= 4096:
                value = json.loads(raw)
                if isinstance(value, dict) and value.get('status') in ('ok', 'stale', 'unavailable'):
                    status['snapshot_status'] = value['status']
                    updated = value.get('updated_at')
                    status['snapshot_age_seconds'] = max(0, int(time.time()) - updated) if type(updated) is int else None
                else:
                    status['snapshot_status'] = 'invalid; inspect collector status'
        status['credential_file'] = 'present (not inspected)' if auth.is_file() else 'missing; complete native Codex sign-in'
        address = env['METER_API_BIND_ADDRESS']
        if ':' in address:
            address = '[' + address + ']'
        status['endpoint'] = f"http://{address}:{env['METER_API_PORT']}/v2/usage"
        print(json.dumps(status, indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('configure', 'install', 'start', 'stop', 'status', 'uninstall', 'doctor', 'addresses', '_collector', '_api'))
    p.add_argument('--directory', type=Path, default=ROOT / '.local/meter')
    p.add_argument('--launch-agents-dir', type=Path, default=Path.home() / 'Library/LaunchAgents')
    p.add_argument('--bind-address', default='127.0.0.1')
    p.add_argument('--port', type=lambda v: api.integer(v, 1024, 65535), default=8080, metavar='PORT')
    p.add_argument('--auth-file', default='~/.codex/auth.json')
    args = p.parse_args()
    inst = Installation(args.directory)
    try:
        if args.action == 'addresses':
            result = run(['/sbin/ifconfig', '-a'])
            addresses = sorted({v for v in re.findall(r'\binet (\d+\.\d+\.\d+\.\d+)', result.stdout)
                                if not ipaddress.IPv4Address(v).is_loopback})
            print('Local IPv4 candidates (select your trusted Wi-Fi/LAN interface):\n' + '\n'.join(addresses))
        elif args.action == '_collector':
            # exec leaves one native process. Data/state flock protects all entry points.
            inst.manifest()
            inst.configuration()
            os.execve(str(inst.directory / 'meter-collector'), ['meter-collector', '--env-file', str(inst.config)], child_env())
        else:
            with inst.lock():
                if args.action == 'configure':
                    inst.configure(args)
                elif args.action == 'install':
                    inst.install(args.launch_agents_dir.expanduser().absolute())
                elif args.action == '_api':
                    inst.reconcile()
                elif args.action == 'doctor':
                    inst.configuration()
                    docker = shutil.which('docker')
                    if not docker:
                        raise Problem('Docker CLI is missing; install and start Docker Desktop.')
                    inst.docker({'docker': docker})
                    print('Configuration, storage permissions and Docker engine checks passed. Native credentials were not opened.')
                else:
                    getattr(inst, args.action)()
    except (Problem, OSError, ValueError, RuntimeError) as exc:
        message = str(exc) if isinstance(exc, Problem) else 'Configuration or installation unavailable; check the private .env literal values, paths, permissions, and run install.'
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    raise SystemExit(main())
