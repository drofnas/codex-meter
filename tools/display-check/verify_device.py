#!/usr/bin/env python3
"""Check physical render telemetry against oracle-validated fixture expectations.

Human confirmation of the actual LCD remains a separate required gate.
"""
import argparse
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]


def verify(path, manifest_path=ROOT/'artifacts/reset-calendar-days/display/manifest.json'):
    data=json.loads(path.read_text()); assert not data['failures'], data['failures']
    events=data['events']; logs=data['logs']; frames=[]; heartbeats=[]; polls=[]
    for row in logs:
        line=row['line']
        assert not any(w in line.lower() for w in ['panic','guru meditation','watchdog']), line
        m=re.search(r'status=(\w+) quota=(\S+) reset=(.*?) offset=(\S+) due=(\d) age=(.*?) labels=(\S+) coverage=(\S+) values=(\S+) observation=(-?\d+) duration_ms=(\d+) heap=(\d+) uptime=(\d+)', line)
        if m:
            frames.append(dict(elapsed=row['elapsed'],status=m[1],quota=m[2],reset=m[3],offset=m[4],due=int(m[5]),
                               age=m[6],labels=m[7],coverage=m[8],values=m[9],observation=int(m[10]),
                               duration_ms=int(m[11]),heap=int(m[12]),uptime=int(m[13])))
        m=re.search(r'alive uptime=(\d+)s free_heap=(\d+)',line)
        if m: heartbeats.append((int(m[1]),int(m[2])))
        m=re.search(r'poll accepted=(\d) error=(\w+)',line)
        if m: polls.append(dict(elapsed=row['elapsed'],accepted=int(m[1]),error=m[2]))
    assert len(frames)>=10 and len(heartbeats)>=20 and polls, 'missing device evidence'
    assert sum('rst:' in row['line'] for row in logs)<=1, 'unexpected reset'
    assert all(0<b[0]-a[0]<=12 for a,b in zip(heartbeats,heartbeats[1:])), 'heartbeat stalled'
    manifest={i['name']:i['frame'] for i in json.loads(manifest_path.read_text())}
    names=[]
    for e in events:
        if e['mode'].startswith('display_') and e['mode'] not in names: names.append(e['mode'])
    required={'unavailable','normal','zero-remaining','full','unknown-history','zeros-and-fraction','stale',
              'dst-spring','dst-fall','offset-positive','long-date','full-bar','saturday-eight','saturday-five-am','spring-nine','noon','fractional-offset','expires-offline','normal-recovered'}
    assert {n.removeprefix('display_') for n in names}>=required, 'fixture sequence incomplete'
    for mode in names:
        served=[e for e in events if e['mode']==mode]; start=served[0]['elapsed']
        end=next((e['elapsed'] for e in events if e['elapsed']>served[-1]['elapsed'] and e['mode']!=mode),float('inf'))
        name=mode.removeprefix('display_'); expected=manifest['normal' if name=='normal-recovered' else name]
        candidates=[f for f in frames if (0 if name=='unavailable' else start)<=f['elapsed']<end]
        assert any(f['quota']==expected['quota'] and f['reset']==expected['reset'] and
                   f['offset']==expected['offset'] and f['labels']==','.join(b['label'] for b in expected['bars']) and
                   f['values']==','.join(b['value'] for b in expected['bars']) and
                   f['coverage']==''.join(b['coverage'] for b in expected['bars']) and
                   (f['status']==expected['status'] or name=='expires-offline') for f in candidates), ('missing rendered fixture',name,candidates)
    exp=next(e for e in events if e['mode']=='display_expires-offline')
    assert any(f['elapsed']>exp['elapsed'] and f['quota']=='55%' and f['due'] and f['status']=='STALE' and
               f['values'].startswith('30,~15,~0,') for f in frames), 'reset expiry fabricated or lost reading'
    outage=next(e for e in events if e['mode']=='outage')
    assert any(p['elapsed']>=outage['elapsed'] and p['error']=='transport' for p in polls), 'missing stopped service'
    assert any(p['elapsed']>outage['elapsed']+16 and p['accepted'] for p in polls), 'missing recovery'
    return dict(result='passed',rendered_fixture_cases=len(names),render_logs=len(frames),heartbeats=len(heartbeats),
                maximum_render_ms=max(f['duration_ms'] for f in frames),
                free_heap_min_bytes=min(h for _,h in heartbeats), free_heap_first_bytes=heartbeats[0][1],
                free_heap_last_bytes=heartbeats[-1][1],maximum_uptime_seconds=heartbeats[-1][0],
                visual_confirmation='separate required gate')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('events',type=Path); p.add_argument('--manifest',type=Path,default=ROOT/'artifacts/reset-calendar-days/display/manifest.json'); a=p.parse_args()
    print(json.dumps(verify(a.events,a.manifest),indent=2))
