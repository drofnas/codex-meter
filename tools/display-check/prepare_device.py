#!/usr/bin/env python3
"""Prepare synthetic device fixtures after check.py has validated the view cases."""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--native',type=Path,default=ROOT/'artifacts/reset-calendar-days/display')
parser.add_argument('--output',type=Path,default=ROOT/'artifacts/reset-calendar-days/device-fixtures')
args=parser.parse_args()
OUT=args.output.resolve()
if not OUT.is_relative_to(ROOT/'artifacts') or not args.native.resolve().is_relative_to(ROOT/'artifacts'):parser.error('use repository artifacts')
SPEC = importlib.util.spec_from_file_location('oracle', ROOT/'tools/contract-check/validate.py')
oracle = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(oracle)
# Two polls per screen at the temporary 10-second interval give physical review time.
NAMES = ['unavailable', 'normal', 'zero-remaining', 'full', 'unknown-history',
         'zeros-and-fraction', 'stale', 'dst-spring', 'dst-fall', 'offset-positive',
         'long-date', 'full-bar', 'saturday-eight', 'saturday-five-am', 'spring-nine', 'noon', 'fractional-offset', 'expires-offline']
OUT.mkdir(parents=True, exist_ok=True)
plan=[]
for name in NAMES+['normal-recovered']:
    source = 'normal' if name == 'normal-recovered' else name
    m = json.loads((args.native/(source+'.json')).read_text())
    # Distinct synthetic accounts permit historical/DST fixtures in either order.
    if m['scope'] is not None: m['scope'] = hashlib.sha256(name.encode()).hexdigest()
    raw=json.dumps(m,separators=(',', ':')).encode(); oracle.validate(raw,version=2)
    path=OUT/(name+'.json'); path.write_bytes(raw)
    entry={'mode':'display_'+name,'file':str(path.relative_to(ROOT))}
    plan.extend([copy.deepcopy(entry),copy.deepcopy(entry)])
    if name=='expires-offline': plan.append({'mode':'outage'})
(OUT.parent/'display-plan.json').write_text(json.dumps(plan,indent=2)+'\n')
print(f'Prepared {len(plan)} display fixture steps; private settings unchanged.')
