#!/usr/bin/env python3
"""Oracle-checked view semantics, sanitized pixel bounds, and repeatable images.

Run with the contract-check Python environment (jsonschema required).
"""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'artifacts/cm-007/native'
SPEC = importlib.util.spec_from_file_location('oracle', ROOT / 'tools/contract-check/validate.py')
oracle = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(oracle)
sys.path.insert(0,str(ROOT/'tools/contract-check'))
from generate_v2 import calendarize


def fixtures():
    source = ROOT / 'contracts/v2/fixtures'
    result = {Path(i['file']).stem: json.loads((source/i['file']).read_text())
              for i in json.loads((source/'manifest.json').read_text())
              if i.get('kind', 'usage') == 'usage' and i['expect'] == 'valid'}
    normal = result['normal']
    result['full'] = dict(copy.deepcopy(normal), remaining_percent=100)
    result['almost-full'] = dict(copy.deepcopy(normal), remaining_percent=99.6)
    m = copy.deepcopy(normal); m['remaining_percent'] = .4
    m['days'][0]['used_delta_pp'] = 0  # measured zero, partial zero, future zero together
    m['days'][1]['used_delta_pp'] = .2
    result['zeros-and-fraction'] = m
    m = copy.deepcopy(normal); m['days'][0]['used_delta_pp'] = 100
    m['days'][1]['used_delta_pp'] = 0
    result['full-bar'] = m
    for name, zone in [('offset-positive', 'Pacific/Kiritimati'), ('offset-negative', 'Etc/GMT+12')]:
        m = copy.deepcopy(normal); m['timezone'] = zone
        m['reset_local'] = oracle.reset_text(m['reset_at'], ZoneInfo(zone))
        for d in m['days']:
            d['label'] = oracle.LABELS[oracle.datetime.fromtimestamp(d['start_at'], ZoneInfo(zone)).weekday()]
        result[name] = calendarize(m)
    m = copy.deepcopy(normal); shift = 4070995200 - m['observed_at']
    for k in ['observed_at', 'updated_at', 'as_of', 'reset_at']: m[k] += shift
    for k in ['start_at', 'end_at']: m['cycle'][k] += shift
    m['reset_local'] = oracle.reset_text(m['reset_at'], ZoneInfo(m['timezone']))
    for d in m['days']:
        d['start_at'] += shift; d['end_at'] += shift
        d['label'] = oracle.LABELS[oracle.datetime.fromtimestamp(d['start_at'], ZoneInfo(m['timezone'])).weekday()]
    result['long-date'] = calendarize(m)
    m = copy.deepcopy(normal)
    m['observed_at'] = m['updated_at'] = m['as_of'] = m['reset_at'] - 5
    for d in m['days']:
        if d['coverage'] == 'future': d.update(coverage='unknown', used_delta_pp=None)
    result['expires-offline'] = m
    return result


def run(output=OUT, landscape_baseline=None):
    OUT = output.resolve()
    if not OUT.is_relative_to(ROOT / 'artifacts'):
        raise ValueError('Use an output directory under repository artifacts')
    baseline = json.loads(landscape_baseline.read_text()) if landscape_baseline else None
    OUT.mkdir(parents=True, exist_ok=True)
    binary = OUT/'render'
    subprocess.run(['c++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-g',
                    '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-I', str(ROOT),
                    str(Path(__file__).with_name('native.cpp')), '-o', str(binary)], check=True)
    orientation_binary = OUT/'orientation-check'
    subprocess.run(['c++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-g',
                    '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-I', str(ROOT),
                    str(Path(__file__).with_name('orientation.cpp')), '-o', str(orientation_binary)], check=True)
    subprocess.run([str(orientation_binary)], check=True, timeout=15)
    cases = fixtures(); manifest = []; timings = []; frame_bytes = set()
    landscape_checked = 0
    for name, value in cases.items():
        raw = json.dumps(value, separators=(',', ':')).encode()
        oracle.validate(raw, 'usage', version=2)
        fixture = OUT/(name+'.json'); fixture.write_bytes(raw)
        scenarios = [(name, 0, 'online')]
        if name == 'normal': scenarios += [('offline', 181000, 'offline'), ('expired-outage', 604800000, 'offline'), ('very-old', 2**50, 'offline')]
        if name == 'resets-one-second': scenarios += [('resets-expired', 1000, 'online')]
        if name == 'resets-four-days': scenarios += [('resets-cross-red', 1000, 'online')]
        if name == 'resets-blue': scenarios += [('resets-cross-yellow', 86400000, 'offline')]
        if name == 'expires-offline': scenarios += [('expires-after-5s', 6000, 'offline')]
        for label, elapsed, network in scenarios:
            for position, suffix in enumerate(('', '-portrait', '-inverted', '-portrait-inverted')):
                name_with_position = label + suffix
                image = OUT/(name_with_position+'.ppm')
                p = subprocess.run([str(binary), str(fixture), str(image), str(elapsed), network, str(position)],
                                   capture_output=True, text=True, check=True, timeout=15)
                f = json.loads(p.stdout)
                timing = re.search(r'frame_bytes=(\d+) sanitized_render_mean_us=([\d.]+)', p.stderr)
                assert timing, p.stderr
                frame_bytes.add(int(timing[1])); timings.append(float(timing[2]))
                count, expiry = value['resets_available'], value.get('resets_expire_at')
                instant = value['as_of'] + elapsed//1000
                visible = count is not None and count > 0 and (expiry is None or instant < expiry)
                assert f['resets'] == (str(count) if visible else '')
                color = 0xAAB8C2
                if visible and expiry is not None and value['reason'] != 'clock_error':
                    days = (expiry-instant)/86400
                    color = 0xFF6060 if days < 4 else 0xFFE060 if days <= 7 else 0x60BFFF
                assert f['resets_color'] == color
                if value['observed_at'] is None:
                    assert f['status'] == 'WAIT' and f['quota'] == '--%' and f['gauge'] == 0
                    assert f['reset'] == f['offset'] == '--'
                    assert all(b['value'] == '?' and b['label'] == '--' for b in f['bars'])
                else:
                    local=oracle.datetime.fromtimestamp(value['reset_at'],ZoneInfo(value['timezone']))
                    assert f['reset'] == local.strftime('%Y-%m-%d %-I:%M %p')
                    offset=int(local.utcoffset().total_seconds())//60
                    compact=('+' if offset>0 else '-')+str(abs(offset)//60) if offset else '0'
                    if abs(offset)%60: compact+=':'+str(abs(offset)%60).zfill(2)
                    assert f['offset']=='('+compact+')'
                    assert [b['label'] for b in f['bars']] == [d['label'] for d in value['days']]
                    assert f['quota'] != '--%' and 0 <= f['gauge'] <= 296
                    if network == 'offline' or value['status'] == 'stale': assert f['status'] == 'STALE'
                    if value['as_of'] + elapsed//1000 >= value['reset_at']: assert f['due']
                    for b, d in zip(f['bars'], value['days']):
                        expired_future = d['coverage'] == 'future' and d['start_at'] <= value['as_of'] + elapsed//1000
                        if d['coverage'] == 'unknown' or expired_future: assert b['value'] == '?' and b['height'] == 0
                        elif d['coverage'] == 'future': assert b['value'] == '0>' and b['height'] == 0
                        else:
                            assert b['value'].startswith('~') == (d['coverage'] == 'partial')
                            assert (b['height'] == 0) == (d['used_delta_pp'] == 0)
                            assert 0 <= b['height'] <= 38
                    if label.startswith('expired') or label == 'expires-after-5s':
                        assert f['quota'] == '55%' and f['bars'][0]['value'] == '30', 'reset fabricated new quota/history'
                if name == 'full': assert f['quota'] == '100%' and f['gauge'] == 296
                if name == 'almost-full': assert f['quota'] == '>99%'
                if name == 'zeros-and-fraction': assert f['quota'] == '<1%'
                if name == 'zero-remaining': assert f['quota'] == '0%' and f['gauge'] == 0
                if baseline is not None and position == 0:
                    assert hashlib.sha256(image.read_bytes()).hexdigest() == baseline[label], ('landscape pixels changed', label)
                    landscape_checked += 1
                manifest.append(dict(name=name_with_position, scenario=label, position=position,
                                     width=240 if position % 2 else 320, height=320 if position % 2 else 240,
                                     frame=f, ppm=str(image.relative_to(ROOT))))
    assert len(frame_bytes)==1 and max(frame_bytes)<=256
    summary = dict(oracle_fixtures=len(cases), rendered_cases=len(manifest), sanitized_bounds='passed',
                   frame_bytes=max(frame_bytes), iterations_per_case=100, orientations=4,
                   unchanged_landscape_cases=landscape_checked if baseline is not None else None,
                   orientation_input_persistence='passed',
                   sanitized_render_mean_us_min=min(timings), sanitized_render_mean_us_max=max(timings))
    (OUT/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    (OUT/'measurement.json').write_text(json.dumps(summary, indent=2)+'\n')
    print(json.dumps(summary))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, default=OUT)
    p.add_argument('--landscape-baseline', type=Path, help='JSON of pre-change landscape PPM SHA-256 hashes')
    args = p.parse_args()
    run(args.output, args.landscape_baseline)
