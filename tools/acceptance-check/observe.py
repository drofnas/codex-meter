#!/usr/bin/env python3
"""Bounded, incremental real-device observation; private evidence stays local."""
import argparse
import http.client
import json
from pathlib import Path
import re
import subprocess
import threading
import time

import serial
from common import ROOT, Engine, host_sample, meter, output_dir, save, usage

FRAME = re.compile(r'status=(\w+) quota=(\S+) reset=(.*?) offset=(\S+) due=(\d) age=(.*?) labels=(\S+) coverage=(\S+) values=(\S+) observation=(-?\d+) duration_ms=(\d+) heap=(\d+) uptime=(\d+)')
HEARTBEAT = re.compile(r'alive uptime=(\d+)s free_heap=(\d+)')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory', type=Path, default=ROOT / '.local/meter')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--serial-port', default='/dev/cu.wchusbserial10')
    p.add_argument('--seconds', type=int, required=True)
    p.add_argument('--probe-binary', type=Path, required=True)
    p.add_argument('--start', action='store_true', help='Capture baseline, then start the prepared real installation')
    p.add_argument('--reset', action='store_true', help='One intentional board reset at observation start')
    args = p.parse_args()
    if not 30 <= args.seconds <= 90000 or not args.probe_binary.resolve().is_relative_to(ROOT):
        p.error('use 30..90000 seconds and a repository-local comparison probe')
    out = output_dir(args.output)
    inst = meter.Installation(args.directory)
    env, auth = inst.configuration()
    engine = Engine()
    started = time.monotonic()
    epoch = time.time()
    stop = threading.Event()
    frames, heartbeats, failures = [], [], []
    status = {'status': 'running', 'started_at': epoch, 'expected_end_at': epoch + args.seconds,
              'duration_seconds': args.seconds, 'intentional_initial_reset': args.reset,
              'captures_application_startup': args.start}
    save(out / 'status.json', status)
    save(out / 'docker-baseline.json', host_sample(inst))

    def serial_worker():
        try:
            with serial.Serial(port=None, baudrate=115200, timeout=.2) as port, (out / 'serial.jsonl').open('w') as log:
                port.dtr = False; port.rts = False; port.port = args.serial_port; port.open()
                if args.reset:
                    port.rts = True; time.sleep(.1); port.rts = False
                pending = b''
                while not stop.is_set():
                    pending += port.read(4096)
                    if len(pending) > 16384:
                        failures.append('serial_line_overflow'); pending = b''
                    while b'\n' in pending:
                        raw, pending = pending.split(b'\n', 1)
                        line = re.sub(r'\x1b\[[0-9;]*m', '', raw.decode(errors='replace')).strip()
                        if not ('[meter_network:' in line or '[meter_display:' in line or '[smoke_test:' in line or
                                any(w in line for w in ('Guru Meditation', 'panic', 'watchdog', 'rst:'))):
                            continue
                        row = {'epoch': time.time(), 'elapsed': time.monotonic() - started, 'line': line}
                        log.write(json.dumps(row) + '\n'); log.flush()
                        match = FRAME.search(line)
                        if match:
                            frames.append({'epoch': row['epoch'], 'status': match[1], 'quota': match[2], 'reset': match[3],
                                           'offset': match[4], 'observation': int(match[10]), 'heap': int(match[12]),
                                           'uptime': int(match[13])})
                        match = HEARTBEAT.search(line)
                        if match:
                            heartbeats.append({'epoch': row['epoch'], 'uptime': int(match[1]), 'heap': int(match[2])})
                        if any(w in line.lower() for w in ('panic', 'guru meditation', 'watchdog')):
                            failures.append('device_runtime_failure')
        except Exception as exc:
            failures.append('serial_' + type(exc).__name__)

    worker = threading.Thread(target=serial_worker, daemon=True)
    worker.start()
    start_process = None
    samples, probes = 0, 0
    latest_api = None
    latest_usage = None
    startup_max = 0
    next_host, next_api = started, started
    last_probe_observation = None
    try:
        if args.start:
            start_process = subprocess.Popen([str(Path(inst.manifest()['python'])), str(ROOT / 'scripts/meter.py'), 'start',
                                               '--directory', str(inst.directory)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with (out / 'host.jsonl').open('w') as hostlog, (out / 'usage.jsonl').open('w') as usagelog, (out / 'source.jsonl').open('w') as sourcelog:
            while time.monotonic() - started < args.seconds:
                now = time.monotonic(); elapsed = now - started
                if now >= next_api:
                    try:
                        latest_api = engine.sample(inst.identity + '-api-1')
                        latest_usage = usage(env)
                        usagelog.write(json.dumps({'epoch': time.time(), 'value': latest_usage}) + '\n'); usagelog.flush()
                        status['api_error'] = None
                    except (OSError, ValueError, KeyError, http.client.HTTPException) as exc:
                        status['api_error'] = type(exc).__name__
                        engine.close()
                    next_api = time.monotonic() + 5 if elapsed < 120 else time.monotonic() + 60
                if now >= next_host:
                    extra = (start_process.pid,) if start_process and start_process.poll() is None else ()
                    row = host_sample(inst, extra)
                    row.update(epoch=time.time(), elapsed=time.monotonic() - started, api=latest_api)
                    row['combined_rss_bytes'] = row['host_rss_bytes'] + (latest_api['rss_bytes'] if latest_api else 0)
                    hostlog.write(json.dumps(row) + '\n'); hostlog.flush(); samples += 1
                    if elapsed < 120:
                        startup_max = max(startup_max, row['combined_rss_bytes'])
                    next_host = time.monotonic() + (.1 if elapsed < 15 else 1 if elapsed < 120 else 60)
                    status.update(elapsed_seconds=elapsed, host_samples=samples, frames=len(frames), heartbeats=len(heartbeats),
                                  failures=sorted(set(failures)), startup_sampled_peak_mib=startup_max / 1048576 if args.start else None,
                                  last_frame=frames[-1] if frames else None, last_heartbeat=heartbeats[-1] if heartbeats else None,
                                  latest_combined_rss_mib=row['combined_rss_bytes'] / 1048576)
                    save(out / 'status.json', status)
                if latest_usage and latest_usage.get('observed_at') is not None and latest_usage['observed_at'] != last_probe_observation:
                    # Independent read-only reference; never retain raw credentials or response bodies.
                    try:
                        result = subprocess.run([str(args.probe_binary.resolve()), '--auth-file', str(auth)],
                                                capture_output=True, timeout=15, env=meter.child_env())
                        value = json.loads(result.stdout)
                    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                        value = {'reference_unavailable': type(exc).__name__}
                    sourcelog.write(json.dumps({'epoch': time.time(), 'value': value}) + '\n'); sourcelog.flush(); probes += 1
                    last_probe_observation = latest_usage['observed_at']
                if failures:
                    raise ValueError('observation_failure')
                time.sleep(.05)
        status['status'] = 'complete'
    except BaseException as exc:
        status.update(status='failed', error=type(exc).__name__)
        raise
    finally:
        stop.set(); worker.join(3); engine.close()
        status.update(finished_at=time.time(), elapsed_seconds=time.monotonic() - started, frames=len(frames),
                      heartbeats=len(heartbeats), source_probes=probes, failures=sorted(set(failures)))
        save(out / 'status.json', status)
        print(json.dumps({k: v for k, v in status.items() if k not in ('last_frame', 'last_heartbeat')}, indent=2))


if __name__ == '__main__':
    main()
