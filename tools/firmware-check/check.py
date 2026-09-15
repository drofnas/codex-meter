#!/usr/bin/env python3
"""Sanitized native firmware contract, state, framing and deadline checks."""
import json
from pathlib import Path
import socket
import subprocess
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'artifacts/reset-calendar-days/firmware'


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    binary = OUT / 'check'
    subprocess.run(['c++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-g',
                    '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-I', str(ROOT),
                    str(Path(__file__).with_name('native.cpp')), '-o', str(binary)], check=True)
    def parse(raw):
        path = OUT / 'candidate.json'
        path.write_bytes(raw)
        return subprocess.check_output([str(binary), str(path)], text=True, timeout=3).strip() == 'valid'
    fixtures = ROOT / 'contracts/v2/fixtures'
    # The API owns the IANA DB and derives valid zone-specific labels/offsets.
    # The device validates these fields' shapes and reset epoch/offset arithmetic.
    host_localization = {'bad-timezone.json', 'bad-label.json', 'wrong-calendar-boundary.json'}
    passed = 0
    for item in json.loads((fixtures / 'manifest.json').read_text()):
        if item.get('kind', 'usage') != 'usage': continue
        expected = item['expect'] == 'valid' or item['file'] in host_localization
        actual = parse((fixtures / item['file']).read_bytes())
        assert actual == expected, (item['file'], expected, actual)
        passed += 1
    normal = json.loads((fixtures / 'normal.json').read_text())
    encode = lambda value: json.dumps(value, separators=(',', ':')).encode()
    good = encode(normal)
    mutations = [b'', b'null', b'[]', good + b'{}', good + b'\0', good[:-1],
                 b'[' * 4096, b' ' * 4097, good.replace(b'"version":2', b'"version":02'),
                 good.replace(b'"version":2', b'"version":2.'),
                 good.replace(b'"version":2', b'"version":2e'),
                 good.replace(b'"version":2', b'"version":true'),
                 good.replace(b'"version":2', b'"version":2,"\\u0076ersion":2'),
                 good.replace(b'"bucket":"codex"', b'"bucket":"codex\\u0000"')]
    for raw in mutations: assert not parse(raw), 'malformed accepted'
    assert parse(good.replace(b'"version":2', b'"version":2.0'))
    assert parse(good.replace(b'"bucket":"codex"', b'"bucket":"co\\u0064ex"'))
    assert parse(good + b' ' * (4096 - len(good)))
    assert not parse(good + b' ' * (4097 - len(good)))
    for key in normal:
        m = dict(normal); m.pop(key); assert not parse(encode(m)), key
    subprocess.run([str(binary), 'state', str(fixtures / 'normal.json')], check=True, timeout=20)
    subprocess.run([str(binary), 'fuzz', str(fixtures / 'normal.json')], check=True, timeout=20)
    token = 'a' * 64
    scenarios = [
        ('length', b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}', 'valid'),
        ('chunked', b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n1\r\n{\r\n1\r\n}\r\n0\r\n\r\n', 'valid'),
        ('status', b'HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n', 'http_status'),
        ('redirect', b'HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:9\r\nContent-Length: 0\r\n\r\n', 'http_status'),
        ('oversized', b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 4097\r\n\r\n', 'oversized'),
        ('chunk-overflow', b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n1001\r\n', 'oversized'),
        ('truncated', b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 3\r\n\r\n{}', 'truncated'),
        ('gzip', b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Encoding: gzip\r\nContent-Length: 2\r\n\r\n{}', 'encoding'),
        ('no-framing', b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{}', 'headers'),
        ('ambiguous-framing', b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n', 'headers'),
        ('duplicate-length', b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}', 'headers'),
        ('header-cap', b'HTTP/1.1 200 OK\r\n' + b'X-Long: ' + b'a'*2048, 'headers'),
        ('slow-headers', b'HTTP/1.1 200 OK\r\n', 'timeout'),
        ('slow-body', b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 10\r\n\r\n', 'timeout'),
    ]
    durations = []
    for name, response, expected in scenarios:
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0)); server.listen(1); server.settimeout(5)
            port = server.getsockname()[1]
            failures = []
            def serve():
                try:
                    with server.accept()[0] as peer:
                        peer.settimeout(2); request = b''
                        while b'\r\n\r\n' not in request:
                            part = peer.recv(512)
                            if not part: raise RuntimeError('incomplete test request')
                            request += part
                        assert request.startswith(b'GET /v2/usage HTTP/1.1\r\n')
                        assert b'Authorization: Bearer ' + token.encode() + b'\r\n' in request
                        peer.sendall(response)
                        if name.startswith('slow-'):
                            for _ in range(30):
                                time.sleep(.03)
                                try: peer.sendall(b' ')
                                except OSError: break
                except Exception as error: failures.append(type(error).__name__)
            worker = threading.Thread(target=serve); worker.start()
            start = time.monotonic()
            result = subprocess.check_output([str(binary), 'http', '127.0.0.1', str(port), token, '300'], text=True, timeout=3).strip()
            elapsed = time.monotonic() - start; durations.append(elapsed)
            worker.join(5)
            assert not worker.is_alive() and not failures, (name, failures)
            assert result == expected, (name, expected, result)
            assert elapsed < 1.5, (name, elapsed)
    print(json.dumps({'usage_fixture_cases': passed, 'malformed_mutations': len(mutations),
                      'missing_field_mutations': len(normal), 'http_cases': len(scenarios),
                      'maximum_http_test_seconds': round(max(durations), 3),
                      'host_localization_delegated': sorted(host_localization)}))


if __name__ == '__main__': main()
