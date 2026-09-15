"""Generate deterministic calendar-contract fixtures; all values are synthetic."""
import copy
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import validate as oracle

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'contracts/v2/fixtures'

from calendar_fixture import calendarize, scenario

def main():
    OUT.mkdir(parents=True,exist_ok=True);cases={}
    for item in json.loads((ROOT/'contracts/v1/fixtures/manifest.json').read_text()):
        if item['expect']=='valid' and item.get('kind','usage')=='usage':
            cases[Path(item['file']).stem]=calendarize(json.loads((ROOT/'contracts/v1/fixtures'/item['file']).read_text()))
    for name,reset,zone in [('saturday-eight','2026-09-19T13:20:00','America/Los_Angeles'),('saturday-five-am','2026-09-19T05:00:00','America/Los_Angeles'),('spring-nine','2026-03-15T00:30:00','America/Los_Angeles'),('fall-eight','2026-11-07T05:00:00','America/Los_Angeles'),('noon','2026-09-19T12:00:00','America/Los_Angeles'),('fractional-offset','2026-09-19T12:59:00','Asia/Kathmandu')]:
        cases[name]=scenario(cases['normal'],reset,zone)
    # Optional CM-009 field: old v2 snapshots remain valid without it.
    for name,seconds in [('resets-blue',8*86400),('resets-seven-days',7*86400),('resets-four-days',4*86400),('resets-red',4*86400-1),('resets-one-second',1)]:
        cases[name]=dict(copy.deepcopy(cases['normal']),resets_available=2,resets_expire_at=cases['normal']['observed_at']+seconds)
    cases['resets-zero']=dict(copy.deepcopy(cases['normal']),resets_available=0)
    cases['resets-unknown']=dict(copy.deepcopy(cases['normal']),resets_available=None)
    cases['resets-null-expiry']=dict(copy.deepcopy(cases['normal']),resets_expire_at=None)
    cases['resets-max-count']=dict(copy.deepcopy(cases['resets-blue']),resets_available=2147483647)
    cases['resets-stale']=dict(copy.deepcopy(cases['stale']),resets_expire_at=cases['normal']['observed_at']+2*86400)
    manifest=[]
    for name,v in cases.items():
        raw=oracle.encode(v);oracle.validate(raw,version=2)
        (OUT/(name+'.json')).write_bytes(raw+b'\n');manifest.append(dict(file=name+'.json',expect='valid'))
    def invalid(name,mutation,expect='semantics'):
        v=copy.deepcopy(cases['normal']);mutation(v)
        (OUT/(name+'.json')).write_bytes(oracle.encode(v)+b'\n');manifest.append(dict(file=name+'.json',expect=expect))
    invalid('bad-version',lambda v:v.update(version=1),'version')
    invalid('six-days',lambda v:v.update(days=v['days'][:6]))
    invalid('ten-days',lambda v:v.update(days=v['days']+[v['days'][0]]*3),'schema')
    invalid('bad-slot-start',lambda v:v['days'][1].update(start_at=v['days'][1]['start_at']+1))
    invalid('bad-slot-end',lambda v:v['days'][0].update(end_at=v['days'][0]['end_at']-1))
    invalid('reversed-interval',lambda v:v['days'][0].update(end_at=v['days'][0]['start_at']))
    invalid('wrong-calendar-boundary',lambda v:(v['days'][0].update(end_at=v['days'][0]['end_at']+60),v['days'][1].update(start_at=v['days'][1]['start_at']+60)))
    invalid('bad-label',lambda v:v['days'][0].update(label='F'))
    invalid('bad-timezone',lambda v:v.update(timezone='Mars/Olympus'))
    invalid('unclosed-complete',lambda v:v['days'][2].update(coverage='complete',used_delta_pp=0))
    invalid('excess-total',lambda v:v['days'][0].update(used_delta_pp=100))
    invalid('unknown-as-zero',lambda v:v['days'][1].update(coverage='unknown',used_delta_pp=0))
    invalid('future-as-null',lambda v:v['days'][-1].update(used_delta_pp=None))
    invalid('missing-end',lambda v:v['days'][0].pop('end_at'),'schema')
    invalid('extra-field',lambda v:v.update(secret='synthetic'),'schema')
    invalid('resets-bad-expiry',lambda v:v.update(resets_expire_at='soon'),'schema')
    invalid('resets-fractional-expiry',lambda v:v.update(resets_expire_at=v['observed_at']+0.5),'schema')
    invalid('resets-expiry-without-count',lambda v:v.update(resets_available=None,resets_expire_at=v['observed_at']+86400))
    invalid('resets-expiry-with-zero',lambda v:v.update(resets_available=0,resets_expire_at=v['observed_at']+86400))
    invalid('resets-expiry-before-observation',lambda v:v.update(resets_expire_at=v['observed_at']))
    raw=oracle.encode(cases['normal'])
    for name,data,expect in [('duplicate-key',raw.replace(b'"version":2',b'"version":2,"version":2'),'malformed'),('oversized',b'"'+b'x'*4095+b'"','oversized'),('malformed',raw[:-1],'malformed')]:
        (OUT/(name+'.json')).write_bytes(data);manifest.append(dict(file=name+'.json',expect=expect))
    (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    oracle.check_fixtures(2)
if __name__=='__main__':main()
