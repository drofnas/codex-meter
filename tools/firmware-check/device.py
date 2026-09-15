#!/usr/bin/env python3
"""Bounded CYD fault-fixture service; no provider credentials or raw HTTP logs.

Use an interpreter with pyserial. Settings are ignored JSON with address, port,
and a dedicated token. The caller separately installs the test firmware.
"""
import argparse
import copy
from datetime import datetime
import hmac
import json
from pathlib import Path
import re
import socket
import threading
import time
import sys
from zoneinfo import ZoneInfo

import serial

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools/contract-check'))
from calendar_fixture import calendarize

SCENARIOS = ['good', 'malformed', 'good', 'oversized', 'good', 'chunked', 'truncated',
             'good', 'version', 'good', 'unauthorized', 'good', 'timeout', 'good',
             'frozen', 'frozen', 'frozen', 'frozen', 'good', 'outage', 'good',
             'good', 'good', 'good', 'good']


def sample(sequence):
    m = json.loads((ROOT / 'contracts/v2/fixtures/normal.json').read_text())
    now = int(time.time()); shift = now - m['observed_at']
    for k in ['observed_at', 'updated_at', 'as_of', 'reset_at']: m[k] += shift
    for k in ['start_at', 'end_at']: m['cycle'][k] += shift
    zone = ZoneInfo(m['timezone'])
    text = datetime.fromtimestamp(m['reset_at'], zone).strftime('%Y-%m-%d %H:%M %z')
    m['reset_local'] = text[:-2] + ':' + text[-2:]
    for d in m['days']:
        d['start_at'] += shift; d['end_at'] += shift
        d['label'] = ['M', 'T', 'W', 'Th', 'F', 'Sa', 'Su'][datetime.fromtimestamp(d['start_at'], zone).weekday()]
    m['stale_after_seconds'] = 30
    m['remaining_percent'] = 70 - sequence % 20
    return calendarize(m)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--settings', type=Path, required=True)
    p.add_argument('--serial-port', required=True)
    p.add_argument('--seconds', type=int, default=480)
    p.add_argument('--output', type=Path, default=ROOT / 'artifacts/cm-006/device')
    p.add_argument('--reset', action='store_true', help='Reset the CYD at capture start for reproducible test timing')
    p.add_argument('--steady', action='store_true', help='Serve fresh readings only for a final-build smoke check')
    p.add_argument('--display-plan', type=Path, help='Generated, oracle-validated display fixture sequence under artifacts/')
    a = p.parse_args()
    out = a.output.resolve()
    if not out.is_relative_to(ROOT / 'artifacts'): p.error('output must be under repository artifacts')
    out.mkdir(parents=True, exist_ok=True)
    settings = json.loads(a.settings.read_text())
    display_plan = []
    if a.display_plan:
        if a.steady: p.error('--steady and --display-plan cannot be combined')
        if not a.display_plan.resolve().is_relative_to(ROOT / 'artifacts'): p.error('display plan must be under artifacts/')
        for entry in json.loads(a.display_plan.read_text()):
            mode = entry['mode']
            if mode != 'outage' and not re.fullmatch(r'display_[a-z0-9_-]+', mode): p.error('invalid display mode')
            value = None
            if mode != 'outage':
                path = (ROOT / entry['file']).resolve()
                if not path.is_relative_to(ROOT / 'artifacts'): p.error('display fixture must be under artifacts/')
                value = json.loads(path.read_text())
            display_plan.append((mode, value))
        if not display_plan or display_plan[-1][0] == 'outage': p.error('display plan needs a final reading')
    token = settings['token'].encode(); events = []; logs = []; failures = []
    stop = threading.Event(); start = time.monotonic(); end = start + a.seconds
    def serial_worker():
        try:
            with serial.Serial(port=None, baudrate=115200, timeout=.2) as port:
                port.dtr = False; port.rts = False; port.port = a.serial_port; port.open()
                if a.reset:
                    port.rts = True; time.sleep(.1); port.rts = False
                pending = b''
                with (out / 'serial.log').open('w') as log:
                    while not stop.is_set():
                        pending += port.read(4096)
                        while b'\n' in pending:
                            raw, pending = pending.split(b'\n', 1)
                            line = re.sub(r'\x1b\[[0-9;]*m', '', raw.decode(errors='replace')).strip()
                            # Persist only fixed diagnostic lines; exclude Wi-Fi/address configuration.
                            if ('[meter_network:' in line or '[meter_display:' in line or '[smoke_test:' in line or
                                    any(s in line for s in ['Guru Meditation', 'panic', 'watchdog', 'rst:'])):
                                logs.append({'elapsed': round(time.monotonic()-start, 3), 'line': line})
                                log.write(line+'\n'); log.flush()
                                print(line, flush=True)
        except Exception as error: failures.append('serial_' + type(error).__name__)
    worker = threading.Thread(target=serial_worker)
    sequence = 0; last = None
    def listener():
        server = socket.socket(); server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((settings['address'], settings['port'])); server.listen(1); server.settimeout(.3)
        return server
    server = listener()
    worker.start()
    print('Fixture service ready; dedicated bearer authentication enabled.', flush=True)
    try:
        while time.monotonic() < end and not failures:
            mode = 'good' if a.steady or sequence >= len(SCENARIOS) else SCENARIOS[sequence]
            if display_plan: mode = display_plan[min(sequence, len(display_plan)-1)][0]
            if mode == 'outage':
                server.close(); events.append({'elapsed': round(time.monotonic()-start, 3), 'mode': mode})
                time.sleep(16); server = listener(); sequence += 1; continue
            try: peer, _ = server.accept()
            except socket.timeout: continue
            with peer:
                peer.settimeout(3); raw = b''; request_deadline = time.monotonic() + 3
                try:
                    while b'\r\n\r\n' not in raw and len(raw) < 2048:
                        left = request_deadline - time.monotonic()
                        if left <= 0: break
                        peer.settimeout(left)
                        part = peer.recv(512)
                        if not part: break
                        raw += part
                    auth = [line[22:] for line in raw.split(b'\r\n') if line.startswith(b'Authorization: Bearer ')]
                    if (not raw.startswith(b'GET /v2/usage HTTP/1.1\r\n') or len(auth) != 1 or
                            not hmac.compare_digest(auth[0], token)):
                        peer.sendall(b'HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n'); continue
                    if display_plan:
                        last = display_plan[min(sequence, len(display_plan)-1)][1]
                    elif mode in ['good', 'chunked']:
                        last = sample(sequence)
                        if a.steady: last['stale_after_seconds'] = 180
                    value = copy.deepcopy(last or sample(sequence))
                    payload = json.dumps(value, separators=(',', ':')).encode()
                    if mode == 'malformed': payload = b'{'
                    if mode == 'version': value['version'] = 3; payload = json.dumps(value).encode()
                    headers = b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n'
                    event = {'elapsed': round(time.monotonic()-start, 3), 'mode': mode,
                             'observation': value['observed_at'], 'remaining': value['remaining_percent']}
                    events.append(event)
                    print(json.dumps({'fixture': mode, 'sequence': sequence}), flush=True)
                    if mode == 'timeout': time.sleep(3)
                    elif mode == 'unauthorized': peer.sendall(b'HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n')
                    elif mode == 'oversized': peer.sendall(headers+b'Content-Length: 4097\r\n\r\n')
                    elif mode == 'chunked':
                        chunks = [payload[:100], payload[100:]]
                        peer.sendall(headers + b'Transfer-Encoding: chunked\r\n\r\n' +
                                     b''.join(f'{len(c):x}\r\n'.encode()+c+b'\r\n' for c in chunks)+b'0\r\n\r\n')
                    else:
                        claimed = len(payload) + (10 if mode == 'truncated' else 0)
                        peer.sendall(headers+f'Content-Length: {claimed}\r\n\r\n'.encode()+payload)
                    sequence += 1
                except (OSError, TimeoutError): failures.append('fixture_io')
    finally:
        server.close(); stop.set(); worker.join(3)
        (out / 'events.json').write_text(json.dumps({'events': events, 'logs': logs, 'failures': failures}, indent=2)+'\n')
    if failures: raise SystemExit('Device fixture run failed: '+','.join(failures))
    if not events or not any('poll accepted=1' in line['line'] for line in logs):
        raise SystemExit('No accepted CYD readings: check private Wi-Fi/LAN configuration.')
    print(json.dumps({'requests': sum(e['mode'] != 'outage' for e in events), 'diagnostic_lines': len(logs)}))


if __name__ == '__main__': main()
