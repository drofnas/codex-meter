"""Read-only, bounded helpers for real Mac/Docker/CYD acceptance evidence."""
import http.client
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import meter


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


metrics = module('probe_metrics', ROOT / 'tools/collector-probe/measure.py')


def output_dir(path):
    path = path.resolve()
    if not path.is_relative_to(ROOT / 'artifacts'):
        raise ValueError('output must be under repository artifacts')
    path.mkdir(parents=True, mode=0o700, exist_ok=False)
    return path


def save(path, value):
    meter.write(path, (json.dumps(value, indent=2) + '\n').encode())


def host_rows():
    raw = subprocess.check_output(['ps', '-axo', 'pid=,ppid=,rss=,time=,command='], text=True, timeout=5)
    rows = {}
    for line in raw.splitlines():
        f = line.split(None, 4)
        if len(f) == 5:
            rows[int(f[0])] = {'parent': int(f[1]), 'rss_bytes': int(f[2]) * 1024,
                              'cpu_seconds': metrics.cpu_seconds(f[3]), 'command': f[4]}
    return rows


def descendants(rows, roots):
    selected = set(roots)
    while True:
        more = selected | {pid for pid, row in rows.items() if row['parent'] in selected}
        if more == selected:
            break
        selected = more
    return {pid: rows[pid] for pid in selected if pid in rows}


def host_sample(inst, extra_roots=()):
    rows = host_rows()
    roots = [pid for pid, r in rows.items() if
             r['command'].startswith('meter-collector --env-file ' + str(inst.config)) or
             (str(ROOT / 'scripts/meter.py') + ' _api --directory ' + str(inst.directory)) in r['command']]
    tree = descendants(rows, [*roots, *extra_roots])
    # Retain only selected runtime metadata; never persist the host command inventory.
    selected = {str(pid): {'parent': r['parent'], 'rss_bytes': r['rss_bytes'],
                          'cpu_seconds': r['cpu_seconds'], 'executable': Path(r['command'].split()[0]).name}
                for pid, r in tree.items()}
    vm = {str(pid): {'rss_bytes': r['rss_bytes'], 'cpu_seconds': r['cpu_seconds']}
          for pid, r in rows.items() if any(name in r['command'].split()[0] for name in
          ('com.docker.virtualization', 'com.docker.backend', 'com.docker.build'))}
    return {'processes': selected, 'host_rss_bytes': sum(r['rss_bytes'] for r in selected.values()),
            'docker_host_processes': vm, 'docker_host_rss_bytes': sum(r['rss_bytes'] for r in vm.values())}


class Engine(http.client.HTTPConnection):
    def __init__(self):
        result = subprocess.run(['docker', 'context', 'inspect'], capture_output=True, timeout=10, check=True)
        endpoint = json.loads(result.stdout)[0]['Endpoints']['docker']['Host']
        if not endpoint.startswith('unix://'):
            raise ValueError('measurement requires the local Docker Unix socket')
        super().__init__('localhost', timeout=5)
        self.path = endpoint[7:]

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect(self.path)

    def get(self, path):
        self.request('GET', path)
        response = self.getresponse()
        raw = response.read(1048577)
        if response.status != 200 or len(raw) > 1048576:
            raise ValueError('docker_read_unavailable')
        return json.loads(raw)

    def sample(self, name):
        stats = self.get('/containers/' + name + '/stats?stream=false&one-shot=true')
        top = self.get('/containers/' + name + '/top?ps_args=' + quote('-o pid,rss,args'))
        rows = top['Processes']
        if len(rows) != 1 or '/meter-api' not in rows[0][-1]:
            raise ValueError('unexpected_container_process_tree')
        return {'cpu_ns': stats['cpu_stats']['cpu_usage']['total_usage'],
                'rss_bytes': int(rows[0][1]) * 1024, 'pid': int(rows[0][0]),
                'cgroup_memory_bytes': stats['memory_stats']['usage']}


def usage(settings):
    connection = http.client.HTTPConnection(settings['METER_API_BIND_ADDRESS'], int(settings['METER_API_PORT']), timeout=5)
    try:
        connection.request('GET', '/v2/usage', headers={'Authorization': 'Bearer ' + settings['METER_API_TOKEN']})
        response = connection.getresponse()
        raw = response.read(4097)
        if response.status != 200 or len(raw) > 4096:
            raise ValueError('usage_unavailable')
        return json.loads(raw)
    finally:
        connection.close()
