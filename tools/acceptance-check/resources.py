#!/usr/bin/env python3
"""Measure the live native collector, Docker API and every periodic job child."""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

from common import ROOT, Engine, host_sample, meter, output_dir, save


def parse_time(raw):
    times = re.search(r'([0-9.]+) real\s+([0-9.]+) user\s+([0-9.]+) sys', raw)
    rss = re.search(r'(\d+)\s+maximum resident set size', raw)
    if not times or not rss:
        raise ValueError('missing_command_resource_accounting')
    return {'wall_seconds': float(times[1]), 'cpu_seconds': float(times[2]) + float(times[3]),
            'largest_individual_rss_bytes': int(rss[1])}


def summarize(samples, jobs, seconds):
    if seconds < 900 or len(samples) < 180 or not jobs:
        raise ValueError('incomplete_15_minute_window')
    first, last = samples[0], samples[-1]
    elapsed = last['elapsed'] - first['elapsed']
    if elapsed < seconds or any(b['elapsed'] - a['elapsed'] > 6 for a, b in zip(samples, samples[1:])):
        raise ValueError('missing_resource_samples')
    native = [s['collector_cpu_seconds'] for s in samples]
    api = [s['api']['cpu_ns'] for s in samples]
    if any(b < a for values in (native, api) for a, b in zip(values, values[1:])):
        raise ValueError('runtime_restarted_during_measurement')
    cpu = native[-1] - native[0] + (api[-1] - api[0]) / 1e9 + sum(j['cpu_seconds'] for j in jobs)
    maximum = max(s['combined_rss_bytes'] for s in samples)
    return {'elapsed_seconds': elapsed, 'samples': len(samples), 'periodic_jobs': len(jobs),
            'maximum_sampled_combined_rss_mib': maximum / 1048576,
            'cpu_seconds': cpu, 'cpu_percent_one_core': cpu / elapsed * 100,
            'memory_pass': maximum < 64 * 1048576, 'cpu_pass': cpu / elapsed * 100 < 1,
            'rss_definition': 'Simultaneous native process-tree RSS plus Linux API RSS. Five-second samples and 20 Hz sampling while a periodic job runs; latest API sample at most five seconds old.',
            'cpu_definition': 'Native ps CPU delta + API cgroup CPU delta + timed periodic job user/system CPU, including reaped short-lived children. The time wrapper is conservatively included in sampled RSS.',
            'instrumentation': 'Measurement Python, ps, Docker Engine reads and comparison probe excluded. During this window the harness schedules the unchanged API job every 60 seconds so no short-lived child CPU is missed; normal launchd schedule restored afterward.'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory', type=Path, default=ROOT / '.local/meter')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--seconds', type=int, default=900)
    p.add_argument('--warmup', type=int, default=30)
    args = p.parse_args()
    if args.seconds < 900 or args.warmup < 30:
        p.error('require at least 900 seconds after 30-second warmup')
    out = output_dir(args.output)
    inst = meter.Installation(args.directory)
    m = inst.manifest()
    if not inst.loaded('collector') or not inst.loaded('api'):
        raise ValueError('start the real installation first')
    engine = Engine()
    # Stop only the scheduled API job, leaving collector/container running.
    # The exact same command is measured at the normal cadence below.
    if meter.run(['launchctl', 'bootout', inst.target('api')]).returncode:
        raise ValueError('unable_to_suspend_periodic_job')
    samples, jobs = [], []
    active = None
    started = time.monotonic()
    next_sample = started
    next_job = started
    latest_api = None
    warm_baseline = None
    status = {'status': 'running', 'started_at': int(time.time()), 'seconds': args.seconds}
    save(out / 'status.json', status)
    try:
        with (out / 'samples.jsonl').open('w') as log:
            while True:
                now = time.monotonic()
                if active and active.poll() is not None:
                    _, stderr = active.communicate()
                    if active.returncode:
                        raise ValueError('periodic_job_failed')
                    record = parse_time(stderr)
                    if job_started >= args.warmup:
                        jobs.append(record)
                    active = None
                # Don't begin a fresh job across the ending boundary.
                if now >= next_job and now - started < args.warmup + args.seconds:
                    if active:
                        raise ValueError('overlapping_periodic_job')
                    job_started = now - started
                    active = subprocess.Popen(['/usr/bin/time', '-l', m['python'], str(ROOT / 'scripts/meter.py'),
                                               '_api', '--directory', str(inst.directory)],
                                              cwd=ROOT, env=meter.child_env(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, start_new_session=True)
                    next_job += 60
                regular = now >= next_sample
                if regular or active:
                    if regular:
                        latest_api = engine.sample(inst.identity + '-api-1')
                        next_sample += 5
                    row = host_sample(inst, (active.pid,) if active else ())
                    collectors = [v for v in row['processes'].values() if v['executable'] == 'meter-collector']
                    if len(collectors) != 1:
                        raise ValueError('missing_or_duplicate_collector')
                    row.update(elapsed=time.monotonic() - started, epoch=time.time(), api=latest_api,
                               collector_cpu_seconds=collectors[0]['cpu_seconds'],
                               combined_rss_bytes=row['host_rss_bytes'] + latest_api['rss_bytes'])
                    log.write(json.dumps(row) + '\n'); log.flush()
                    if row['elapsed'] >= args.warmup:
                        if warm_baseline is None:
                            warm_baseline = row['elapsed']
                        samples.append(row)
                    status.update(elapsed_seconds=row['elapsed'], samples=len(samples), periodic_jobs=len(jobs),
                                  latest_combined_rss_mib=row['combined_rss_bytes'] / 1048576)
                    if regular:
                        save(out / 'status.json', status)
                if warm_baseline is not None and now - started - warm_baseline >= args.seconds and not active:
                    break
                time.sleep(.05 if active else min(.5, max(0, next_sample - time.monotonic())))
        result = summarize(samples, jobs, args.seconds)
        result['startup_note'] = 'Application startup captured separately by the acceptance observer; this script starts against the already running installation.'
        save(out / 'summary.json', result)
        status.update(status='passed' if result['memory_pass'] and result['cpu_pass'] else 'failed', **result)
    except BaseException as exc:
        status.update(status='failed', error=type(exc).__name__ + ':' + (str(exc) if isinstance(exc, ValueError) else 'measurement_interrupted'))
        raise
    finally:
        if active and active.poll() is None:
            try: os.killpg(active.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            try: active.wait(timeout=15)
            except subprocess.TimeoutExpired:
                try: os.killpg(active.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                active.wait()
        engine.close()
        plist = Path(m['agents']) / (inst.label + '.api.plist')
        restored = meter.run(['launchctl', 'bootstrap', inst.domain, str(plist)]).returncode == 0
        status['normal_schedule_restored'] = restored
        if not restored: status['status'] = 'failed'
        save(out / 'status.json', status)
        print(json.dumps(status, indent=2), flush=True)


if __name__ == '__main__':
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    main()
