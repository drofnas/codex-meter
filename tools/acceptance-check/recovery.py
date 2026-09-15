#!/usr/bin/env python3
"""Remove only this installation's API, hold an outage, then verify recovery."""
import argparse
import http.client
from pathlib import Path
import signal
import time

from common import ROOT, meter, output_dir, save, usage


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory', type=Path, default=ROOT / '.local/meter')
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    out = output_dir(args.output)
    inst = meter.Installation(args.directory)
    m = inst.manifest()
    env, _ = inst.configuration()
    before = usage(env)
    events = []

    def event(name, **values):
        events.append({'event': name, 'epoch': time.time(), **values})
        save(out / 'events.json', events)
        print(name, flush=True)

    if not inst.loaded('collector') or not inst.loaded('api'):
        raise ValueError('both_normal_jobs_must_be_loaded')
    event('before_outage', observation=before.get('observed_at'))
    if meter.run(['launchctl', 'bootout', inst.target('api')]).returncode:
        raise ValueError('unable_to_suspend_api_schedule')
    try:
        if meter.run([m['docker'], 'rm', '-f', inst.identity + '-api-1']).returncode:
            raise ValueError('unable_to_remove_owned_api')
        event('api_removed')
        until = time.monotonic() + 100
        while time.monotonic() < until:
            time.sleep(max(0, min(1, until - time.monotonic())))
    finally:
        plist = Path(m['agents']) / (inst.label + '.api.plist')
        if meter.run(['launchctl', 'bootstrap', inst.domain, str(plist)]).returncode:
            event('schedule_restore_failed')
            raise ValueError('restore_normal_api_schedule_before_continuing')
        event('normal_schedule_restored')
    until = time.monotonic() + 120
    while time.monotonic() < until:
        try:
            after = usage(env)
        except (OSError, ValueError, http.client.HTTPException):
            time.sleep(1)
            continue
        if after.get('status') == 'ok' and after.get('observed_at', 0) >= before.get('observed_at', 0):
            event('fresh_api_recovered', observation=after['observed_at'])
            return
        time.sleep(1)
    raise ValueError('api_did_not_recover')


if __name__ == '__main__':
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    main()
